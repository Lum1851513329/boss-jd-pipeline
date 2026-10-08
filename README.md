<div align="center">

# BOSS 直聘岗位采集流水线

**从「关键词 × 城市」矩阵采集到 JD 补抓、合并去重、Excel 导出的一站式流水线**

按「能中断、能续跑、不丢数据」设计 · 双账号双浏览器并行 · 亿级请求量的实战打磨

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows-0078D4?logo=windows&logoColor=white)](#运行环境)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Upstream](https://img.shields.io/badge/upstream-boss--zhipin--scraper-blueviolet)](NOTICE.md)

</div>

---

> **English abstract** — A production-hardened pipeline for collecting job listings and
> job descriptions (JD) from BOSS直聘 (zhipin.com). It crawls a keyword × city matrix,
> backfills missing JDs in resumable chunks across two parallel browser/account lanes,
> and deduplicates everything into a single CSV/Excel table. Built around two ideas:
> **never lose data** (recovery pool + pid-suffixed files) and **never lie about progress**
> (union-based progress accounting).

---

## 目录

- [为什么需要它](#为什么需要它)
- [核心特性](#核心特性)
- [整体架构](#整体架构)
- [快速开始](#快速开始)
- [数据表结构](#数据表结构)
- [进度统计口径（重要）](#进度统计口径重要)
- [可靠性设计](#可靠性设计)
- [风控识别与应对](#风控识别与应对)
- [排查手册](#排查手册)
- [项目结构](#项目结构)
- [运行环境](#运行环境)
- [常见问题](#常见问题)
- [合规与免责](#合规与免责)

---

## 为什么需要它

抓 BOSS 直聘的岗位数据，看起来只是「请求一个接口、存成 CSV」，实际会连续撞上五堵墙：

| 问题 | 现实表现 |
|---|---|
| **结果上限** | 单个查询只能拿到约 150~300 条，再多就翻不动了 |
| **风控** | 高频访问详情页会触发人机验证，甚至账号级封禁 |
| **JD 拿不到** | 列表接口不返回岗位描述，必须逐个打开详情页，2000+ 条就是 2000+ 次请求 |
| **中断** | 跑几小时的批量任务，会被误杀、断网、休眠、定时任务打断 |
| **数据不一致** | 并发写冲突让文件写不进去，结果散落在多个中间文件里，统计出来的进度是错的 |

这个项目就是把这五件事都解决掉之后沉淀下来的。

## 核心特性

- 🎯 **关键词 × 城市矩阵** — 用 14 个近义关键词 × 5 个城市打破单查询上限，按 `job_id` 全局去重
- 🔀 **双账号 + 双浏览器 + 双向并行** — Chrome 从前往后、Edge 从后往前，**数据源相同方向相反 ⇒ 天然不重复抓同一批岗位**，吞吐翻倍同时把单账号速率减半、显著降低风控概率
- ♻️ **断点续跑，绝不丢数据** — 分块处理、每块立即落盘；启动时自动从 `out_*.json` **恢复池**把已抓到的 JD 收回来，进程被杀/崩溃/断电后重启即可继续
- 🛡️ **崩溃自愈** — 主循环兜底，写进度文件失败这类非关键异常不再终止整个抓取
- 📊 **并集口径的进度统计** — 不拿「主表条数」当进度（那会严重低估），而是合并所有中间产物后按 `job_id` 去重统计
- 🔒 **TCP 端口实例锁** — 不用锁文件（本机删不掉会永久残留），改由 OS 管理，进程退出自动释放
- 🧩 **多进程认领机制** — 认领文件记录 pid，并发实例自动各做不同批次，不会重复抓同一批岗位
- 🚦 **风控识别与降速** — 命中受限关键词自动暂停/停止；软风控可用人工验证快速恢复
- 🔧 **统一配置层** — 所有路径/端口/账号集中在 `scripts/config.py`，全部可用环境变量覆盖，换机器换关键词不用改代码
- 🩺 **内置诊断工具** — 进程排查、风控层级判定、翻页阈值探测、代理探测、详情页取证

## 整体架构

```
              ┌──────────────────────────────────────────────────────────┐
              │  0. 凭据准备        scripts/inject_cookies.py             │
              │     泳道 A: Chrome / CDP 9222 / 锁 9333                   │
              │     泳道 B: Edge   / CDP 9223 / 锁 9335                   │
              │     注入 Cookie → 验证登录态（判据：页面出现明文薪资）      │
              └──────────────────────────┬───────────────────────────────┘
                                         ▼
        ┌────────────────────────────────────────────────────────────────┐
        │  1. 采集列表     scripts/collect_matrix.py                     │
        │     关键词 × 城市 矩阵 · 仅取列表字段（快）· job_id 去重 · 可续跑 │
        └──────────────────────────┬─────────────────────────────────────┘
                                   ▼
        ┌────────────────────────────────────────────────────────────────┐
        │  2. 合并列表主表  scripts/merge_all.py                         │
        │     → <DATASET>.csv（只有列表字段，没有 JD）                    │
        └──────────────────────────┬─────────────────────────────────────┘
                                   ▼
        ┌────────────────────────────────────────────────────────────────┐
        │  3. 补抓 JD      scripts/start_both.py 120    ← 幂等，可反复执行 │
        │                                                                │
        │   forward 泳道 ──┐  分块 (chunk=120)                            │
        │                  ├─ 每块立即落盘 → 中断可续跑                    │
        │   reverse 泳道 ──┘  块间休息 240s（降速抗风控）                  │
        └──────────────────────────┬─────────────────────────────────────┘
                                   ▼
        ┌────────────────────────────────────────────────────────────────┐
        │  4. 看进度       scripts/stat_progress.py                      │
        │     并集口径统计 + 顺带扫描两条日志的风控关键词                  │
        └──────────────────────────┬─────────────────────────────────────┘
                                   ▼
        ┌────────────────────────────────────────────────────────────────┐
        │  5. 收尾合并     scripts/finalize_jd.py                        │
        │     吸收所有时间戳产物 + 两个工作目录的 out_*.json → 主表 + XLSX │
        └──────────────────────────┬─────────────────────────────────────┘
                                   ▼
        ┌────────────────────────────────────────────────────────────────┐
        │  6. 分析报告     scripts/analyze_jobs.py           （可选）      │
        │     薪资分布 / 技能词频 / 城市分布 → Markdown                    │
        └────────────────────────────────────────────────────────────────┘
```

## 快速开始

### 1. 安装依赖

```bash
git clone <this-repo>
cd boss-jd-pipeline
pip install -r requirements.txt
```

### 2. 准备登录 Cookie

从浏览器用 [Cookie-Editor](https://chrome.google.com/webstore/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm)
导出 zhipin.com 的 cookie 为 JSON。如果拿到的是别的格式，可以用脚本转换：

```bash
python scripts/netscape_to_json.py cookies.txt cookies.json   # Netscape 格式（支持 #HttpOnly_ 前缀）
python scripts/kv_to_json.py       cookies.txt cookies.json   # name=value 逐行格式
```

放到 `<工作区>/.boss-agent/cookies_new.json`（泳道 A）和 `cookies_liyoutian.json`（泳道 B）。

### 3. 拉起两个浏览器并注入 Cookie

```bash
# 泳道 A：Chrome
BOSS_BROWSER=chrome BOSS_CDP_PORT=9222 \
  python scripts/inject_cookies.py .boss-agent/cookies_new.json .boss-agent/cdp-profile

# 泳道 B：Edge（不同浏览器 / 不同端口 / 不同 profile）
BOSS_BROWSER=edge BOSS_CDP_PORT=9223 \
  python scripts/inject_cookies.py .boss-agent/cookies_liyoutian.json .boss-agent/edge-profile
```

脚本会验证登录态，**判据是页面上出现明文薪资**（如 `15-25K`）——这比看标题栏可靠得多。

### 4. 采集岗位列表

```bash
python scripts/collect_matrix.py 10 25 10000   # 每轮 10 页 / 轮间隔 25s / 目标 10000 条
python scripts/merge_all.py                    # 合并成列表主表
```

### 5. 补抓 JD（核心）

```bash
python scripts/start_both.py 120
```

**这一条命令可以反复执行**，它是幂等的：已完成就退出、浏览器不在线就拉起、
泳道已在跑就不打扰。适合直接挂到定时任务里。

### 6. 看进度 & 收尾

```bash
python scripts/stat_progress.py --verbose   # 进度 + 风控扫描（只读）
python scripts/finalize_jd.py               # 合并所有产物 → 主表 + Excel
```

## 数据表结构

固定 17 列，`job_id` 是全局主键：

| 列名 | 说明 | 示例 |
|---|---|---|
| `job_id` | 岗位唯一 ID（**去重主键**） | `05d10d1918157441` |
| `岗位名称` | 职位标题 | AI产品经理（大模型应用） |
| `所在城市` | 城市 | 北京 |
| `区域` | 商圈/区 | 朝阳区·望京 |
| `薪资范围` | **明文薪资** | 30-50K·15薪 |
| `公司名称` | 招聘方 | 某某科技 |
| `岗位描述` | **JD 全文**（补抓目标） | 岗位职责：… |
| `工作经验要求` | 从 tags 拆出 | 3-5年 |
| `学历要求` | 从 tags 拆出 | 本科 |
| `岗位链接` | 详情页 URL | https://www.zhipin.com/job_detail/….html |
| `公司规模` | 人数区间 | 500-999人 |
| `融资阶段` | 轮次 | C轮 |
| `所属行业` | 行业 | 人工智能 |
| `技能标签` | 技能关键词 | 大模型,RAG,Agent |
| `福利待遇` | 福利 | 五险一金,年终奖 |
| `Boss活跃度` | HR 活跃状态 | 刚刚活跃 |
| `薪资来源` | `api`（明文） / `list`（列表页解析） | api |

完整样例见 [`docs/sample/jobs_sample.csv`](docs/sample/jobs_sample.csv)（**完全虚构**，仅演示结构）。

## 进度统计口径（重要）

这是本项目**最容易踩、也最值得单独讲**的坑：

**不要拿「主表里 JD 非空的行数」当进度，会严重低估。**

原因有两个：

1. **写冲突会退写成带时间戳的文件。** `save()` 的流程是「写 tmp → `os.replace`」。
   当目标文件被占用（预览面板、另一个实例）时重试 5 次，仍失败就**退写成
   `<主表>_<时间戳>.csv`**。后果是：**数据落盘成功了，但主表还停在旧版本。**
   历史上因为只 glob 了其中一族、漏了另一族，一次性少合并了 **313 条**。
2. **分块产出要到「块结束」才合并进主表。** 块跑到一半时主表一动不动是**正常现象**。

所以正确口径是**并集**：

```
真实进度 = 全部 <DATASET>_含JD*.csv 的并集
         ∪ 两泳道在途 out_*.json 里「不在并集内」的 job_id

forward  = forward 族 CSV ∪ forward 在途
reverse  = (edge 族 CSV ∪ edge 在途) − forward      ← 必须取「独有增量」
```

`reverse` 之所以要减去 `forward`：**edge 主表是从 forward 主表复制起步的**（两边行数相同），
直接相加会把同一批岗位数两遍。

判定是否可信，看 `stat_progress.py` 输出的这一行：

```
越界 job_id = 0   （应为 0，否则并集在虚增）
```

实测输出长这样：

```
DONE=2411/2445 (98.6%)  missing=34
  forward            = 2411
  reverse(独有增量)   = 0
  CSV 并集           = 2411  (forward族 2411 / edge族 2402)
  在途新增           = 0  (forward 0 / edge 0)
  越界 job_id        = 0  （应为 0，否则并集在虚增）

-- 风控扫描 --
  forward: ✅ 无受限关键词
  reverse: ✅ 无受限关键词
```

## 可靠性设计

这套流水线的可靠性来自四个具体机制，不是靠"希望它别崩"：

### 1. 恢复池（`absorb_existing`）

启动时扫描工作目录下所有 `out_*.json`，把已抓到的 JD 收回来合进内存表。
所以进程被误杀、崩溃、断电都**不丢数据**——重启后从恢复池继续。

### 2. 所有共享文件名都带 pid

```
in_<块号>_<pid>.json          输入
out_<块号>_<pid>.json         输出
_jd_done_<方向>_<端口>.json   进度（同时也是恢复池）
```

同一泳道可能有两个实例并发跑。**文件名一共享就 `PermissionError` 崩主循环** ——
这个坑在这个项目里一共出现过三次（`out_*` → `_jd_done_*` → `in_*`），第三次才补全。

### 3. TCP 端口实例锁

不用锁文件：本机安全策略会让 `os.remove()` 失败，锁文件删不掉就永久残留。
TCP 端口由操作系统管理，**进程退出自动释放**。

Windows 上必须显式声明独占，否则默认允许重复绑定、锁形同虚设：

```python
s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
```

### 4. 主循环兜底

```python
while True:
    try:
        main(); break
    except SystemExit:
        raise
    except Exception as exc:
        log(f"⚠️ 主循环异常，10 秒后自动继续：{type(exc).__name__}: {exc}")
        time.sleep(10)
```

写进度文件失败、CDP 抖动、文件被占用……**这些都不该终止一次已经跑了几小时的抓取**。

### 附赠：进程链不是重复实例

正常的单个实例是 **4 个 python 进程的父子链**：

```
backfill_jd.py(持锁) → backfill_jd.py → boss_cdp_raw.py → worker
```

看到一堆 python 别急着清理，**判实例看谁持有锁端口**（9333 / 9335）。

## 风控识别与应对

BOSS 的风控有两个层级，处理方式完全不同：

| 状态 | 症状 | 应对 |
|---|---|---|
| **软风控** | 页面跳转到 `/passport/` 的**安全验证**页，要求人机验证 | **人工在浏览器窗口滑一下即可恢复**，不用等解封时刻 |
| **硬封** | 提示「访问受限」「将于 X 时刻恢复」 | 只能等解封，**不要重试**（重试会加重） |

用对照实验区分**账号级**还是 **IP 级**：

```bash
python scripts/diagnose/probe_block_scope.py
```

- 匿名无 Cookie 正常 + 已登录被拦 → **账号级软风控**（换号或人工验证）
- 两者都被拦 → **IP 级封禁**（需要换出口 IP）

流水线内置的应对：

- **自动识别**：命中 `访问受限 / 暂时无法访问 / 异常行为 / 安全验证 / code 37 / code 31`
  等关键词就暂停 15 分钟；确认「访问受限」则**立即停本轮等待解封**
- **主动降速**：块间休息默认 240s，块大小默认 120 条
- **启动器护栏**：标签页大面积是 `/passport/` 或 `403.html` 时**跳过不启动**，
  避免无效重试把风控加重
- **双账号分摊**：两条泳道并行，单账号速率减半

> ⚠️ **速率波动极大是正常的**：同一夜里实测 1.2s/条 ↔ 39s/条，相差 10 倍以上。
> 单块低产先怀疑文件占用和 CDP 抖动，**连续 2~3 块都低产**才考虑风控。

## 排查手册

```bash
# 进度 + 风控扫描（只读，最常用）
python scripts/stat_progress.py --verbose

# 进程排查
python scripts/diagnose/list_workers.py        # 列出 backfill / scraper 进程及运行时长
python scripts/diagnose/cleanup_workers.py     # 清理这些进程

# 风控排查
python scripts/diagnose/probe_block_scope.py            # 匿名 vs 已登录 → 判断封禁层级
python scripts/diagnose/probe_threshold.py 北京 15 5    # 翻到第几页触发风控
python scripts/diagnose/check_detail_page.py <职位URL>  # 详情页取证（标题 + 正文）

# 网络排查
python scripts/diagnose/probe_proxy.py         # 扫描本机可用代理端口
```

Windows 下查端口属主（实例锁就是端口）：

```bash
netstat -ano | grep -E ":9333|:9335|:9222|:9223"
```

**日志关键词速查**：

| 日志现象 | 含义 | 是否要处理 |
|---|---|---|
| `本块产出偏低（1/120）` + `PermissionError ... .tmp -> .json` | 并发写冲突 | 否，非阻塞 |
| `本块产出偏低` + `TimeoutError: CDP WebSocket recv 超时` | CDP 连接抖动 | 否，重连即可 |
| `恢复已抓 JD N 条` | 恢复池生效，上轮成果已回收 | 否，正常 |
| `第 N 块已被其他实例认领，跳过` | 多实例分工正常 | 否，正常 |
| `主表被占用，已改写 xxx_<时间戳>.csv` | 退写，主表未更新 | 收尾时会被 `finalize_jd.py` 吸收 |
| `访问受限` / `安全验证` | **风控** | 是，见上一节 |

更完整的踩坑清单见 **[`references/pitfalls.md`](references/pitfalls.md)**（改代码前必读），
端到端流程与推荐节奏见 **[`references/workflow.md`](references/workflow.md)**。

## 项目结构

```
boss-jd-pipeline/
├── README.md                    项目说明（本文件）
├── SKILL.md                     Agent Skill 入口（可被 AI 助手直接加载使用）
├── LICENSE                      MIT
├── NOTICE.md                    第三方归属声明与免责声明
├── CHANGELOG.md                 版本变更
├── .gitignore                   ⚠️ 已排除 cookie / 数据 / 中间产物
├── requirements.txt
│
├── scripts/                     ← 本项目代码
│   ├── config.py                ★ 统一配置层（所有路径/端口/账号，env 可覆盖）
│   │
│   ├── inject_cookies.py        注入 Cookie + 拉起常驻 CDP 浏览器 + 验证登录态
│   │
│   ├── collect_matrix.py        关键词×城市 矩阵采集（列表）
│   ├── collect_cities.py        多城市「列表+JD」采集（逐城落盘，可续跑）
│   ├── collect_city.py          单城市 分批+冷却（需 boss-agent CLI）
│   ├── collect2.py              单城市 先取链接再采集（需 boss-agent CLI）
│   │
│   ├── backfill_jd.py           ★ JD 补抓核心（分块/续跑/双向/容错）
│   ├── start_both.py            ★ 双泳道守护启动器（幂等，日常入口）
│   ├── start_backfill.py        单泳道守护启动器
│   │
│   ├── finalize_jd.py           ★ 收尾合并（含时间戳产物）→ 主表 + Excel
│   ├── merge_all.py             原始 CSV → 列表主表（按 job_id 去重）
│   ├── merge_city_csvs.py       多城市 CSV 合并去重（有 JD 优先）
│   │
│   ├── stat_progress.py         ★ 进度统计（并集口径，只读）
│   ├── analyze_jobs.py          市场分析报告（Markdown）
│   ├── netscape_to_json.py      Netscape cookie → JSON
│   ├── kv_to_json.py            name=value cookie → JSON
│   │
│   └── diagnose/                排查工具
│       ├── list_workers.py          列出补抓相关进程
│       ├── cleanup_workers.py       清理这些进程
│       ├── probe_block_scope.py     判断封禁是账号级还是 IP 级
│       ├── probe_threshold.py       探测翻页风控阈值
│       ├── probe_proxy.py           探测本机可用代理
│       └── check_detail_page.py     详情页取证
│
├── scraper/                     ← 上游抓取内核（第三方，MIT，原样内置）
│   ├── boss_cdp_raw.py             抓取主脚本（CDP 旁听页面自身请求）
│   ├── job_summary.py              结果摘要 / 求职提示词生成
│   ├── requirements.txt
│   └── LICENSE.upstream-MIT
│
├── references/
│   ├── workflow.md              端到端流程 · 推荐节奏 · 产物清单
│   └── pitfalls.md              ★ 踩坑清单（6 大类）
│
└── docs/
    └── sample/
        └── jobs_sample.csv      虚构样例数据（演示表结构）
```

## 运行环境

- **操作系统**：Windows（`creationflags` 脱离会话、`SO_EXCLUSIVEADDRUSE` 等为 Windows 特有；
  其他平台需自行移植进程/端口相关代码）
- **Python**：3.10+
- **浏览器**：Chrome（走 Playwright 自带 Chromium）与 Edge（系统安装版），需带界面运行
- **依赖**：见 [`requirements.txt`](requirements.txt)

`collect_city.py` / `collect2.py` 额外需要 `boss-agent` CLI（PyPI 包 `boss_agent_cli`）。
**日常补抓流程不需要它们**，可以用 `BOSS_CLI` 环境变量指向 `boss.exe`。

### 配置

所有路径与参数集中在 `scripts/config.py`，**每一项都能用环境变量覆盖**：

| 环境变量 | 作用 | 默认 |
|---|---|---|
| `BOSS_BASE` | 工作区根目录 | `BOSS_BASE` → `.boss-base` 文件 → 当前目录 |
| `BOSS_DATA_DIR` | 数据目录 | `<BASE>/岗位数据` |
| `BOSS_KEYWORD` | 采集关键词 | `AI产品经理` |
| `BOSS_DATASET` | 数据集名 | `AI产品经理_全量数据` |
| `BOSS_PY` | 子进程解释器 | 自动探测（`.venv` → 已知 venv → 当前） |
| `BOSS_PROXY` | 代理 `socks5://…` | 读 `.boss-agent/proxy.txt` |
| `BOSS_BROWSER` | `chrome` / `edge` | `chrome` |
| `BOSS_CDP_PORT` | 浏览器调试端口 | `9222` |
| `BOSS_PROFILE` | 浏览器 profile 目录 | `<BASE>/.boss-agent/cdp-profile` |
| `BOSS_CHUNK` / `BOSS_BLOCK_REST` | 块大小 / 块间休息秒数 | `120` / `240` |

**工作区根目录 `BASE` 的解析顺序**（`config._default_base()`）：

1. `BOSS_BASE` 环境变量 —— 推荐做法
2. skill 目录下的 `.boss-base` 文件（单行路径，已 gitignore）—— 适合本地固定配置
3. 当前工作目录 —— 兜底，所以请在项目根目录下运行脚本

**仓库里没有任何硬编码的绝对路径**，克隆到任何机器都能直接跑。

例如换关键词：

```bash
BOSS_KEYWORD="数据分析师" BOSS_DATASET="数据分析师_全量数据" \
  python scripts/collect_matrix.py 10 25 5000
```

> `collect_matrix.py` 里的 `KEYWORDS` 列表（近义关键词矩阵）和 `CITIES` 需按需调整；
> 城市名要与 BOSS 站点的城市列表一致才能匹配到数据。

## 常见问题

<details>
<summary><b>注入 Cookie 成功了，但抓到的 JD 全是空 / 页面显示未登录</b></summary>

先看 `inject_cookies.py` 的输出里有没有 `✅ 登录态有效`。判据是**页面上出现明文薪资**
（形如 `15-25K`），不是页面标题。

如果显示未登录：

1. 确认 Cookie 里有 `wt2` 和 `__zp_stoken__`——脚本启动时会自检并打印 `✓/✗`。
   `__zp_stoken__` 常是 **httpOnly** 的，从 Netscape 格式转换时容易被丢掉
   （本项目的 `netscape_to_json.py` 已支持 `#HttpOnly_` 前缀）。
2. Cookie 可能已过期，重新导出。
3. 用 `scripts/diagnose/probe_block_scope.py` 确认是不是被风控了。

</details>

<details>
<summary><b>进度数字一直不动，是不是卡死了？</b></summary>

大概率没有。**分块产出要到「块结束」才合并进主表**，块跑到一半时主表本来就一动不动。

用 `stat_progress.py --verbose` 看「在途新增」和 `out_*.json` 的修改时间。
只要在途文件在长大，就是在正常产出。

</details>

<details>
<summary><b>日志里出现 <code>PermissionError: ... .tmp → .json</code>，要不要处理？</b></summary>

**不用。** 这是两个实例同时写同一个文件导致的，属于已知的非阻塞异常：
该块会只产出很少几条（日志打 `本块产出偏低`），但**已抓到的 JD 会进恢复池，不丢数据**。

想降低发生概率：把定时触发周期从 1 小时改成 2 小时，或把块大小从 120 降到 60
（单块耗时与 1 小时同量级，周期太密会反复打断进行中的块）。

</details>

<details>
<summary><b>能不能不用两个浏览器/两个账号？</b></summary>

可以，用单泳道启动器：

```bash
python scripts/start_backfill.py 120
```

但双泳道不只是快一倍——它把**单账号的请求速率减半**，显著降低触发风控的概率。
如果只有一个账号，也建议至少用 reverse 方向错开。

</details>

<details>
<summary><b>换关键词 / 换城市怎么改？</b></summary>

```bash
BOSS_KEYWORD="数据分析师" BOSS_DATASET="数据分析师_全量数据" python scripts/collect_matrix.py 10 25 5000
```

`collect_matrix.py` 里的 `KEYWORDS` 列表（近义关键词矩阵）和 `CITIES` 需要按需调整。
城市名要与 BOSS 站点的城市列表一致才能匹配到数据。

</details>

<details>
<summary><b>为什么 <code>scraper/</code> 里的代码没有改？</b></summary>

那是上游项目 [eatmoreduck/boss-zhipin-scraper](https://github.com/eatmoreduck/boss-zhipin-scraper)
（MIT）的**原样副本**，故意不做任何修改，这样上游更新时可以干净替换。
本项目只通过 `subprocess` 调用它的命令行接口。详见 [`NOTICE.md`](NOTICE.md)。

</details>

## 合规与免责

- 本项目抓取的是 BOSS 直聘上**公开可见**的招聘信息，仅用于**个人学习与研究**。
- 请遵守 BOSS 直聘的用户协议与 robots 约定，**控制抓取频率**，不要用于批量转售、
  骚扰招聘方或侵犯他人隐私。
- 使用者应自行确保其行为符合所在司法辖区的数据保护与个人信息相关法律
  （如《中华人民共和国个人信息保护法》）。
- 本项目与 BOSS 直聘无任何隶属或合作关系。

⚠️ **上传到公开仓库前请务必确认**：`.gitignore` 已排除 `.boss-agent/`（含账号 Cookie
与浏览器 profile）和抓取数据目录。**Cookie 泄漏等于账号泄漏**，请不要用 `git add -f` 绕过。

## 致谢

- 抓取内核来自 [eatmoreduck/boss-zhipin-scraper](https://github.com/eatmoreduck/boss-zhipin-scraper)（MIT）
- 浏览器自动化基于 [Patchright](https://pypi.org/project/patchright/) / Playwright

## License

[MIT](LICENSE) © 2026 li

> `scraper/` 目录下的文件另行受其原始 MIT 许可证约束，版权归 eatmoreduck 所有。
