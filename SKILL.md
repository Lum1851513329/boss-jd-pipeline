---
name: boss-jd-pipeline
description: "BOSS直聘（zhipin.com）岗位数据采集全流水线：关键词×城市矩阵采集 → JD（岗位描述）批量补抓 → 多产物合并去重 → Excel 导出。内置双账号/双浏览器/双向并行补抓、TCP 端口实例锁、分块续跑与崩溃自愈、软风控识别与降速。Use when the user wants to scrape BOSS直聘/zhipin job listings or JDs, backfill missing 岗位描述, merge/dedupe job CSVs by job_id, or diagnose/restart a running scrape pipeline."
agent_created: true
---

# BOSS直聘 岗位采集流水线

把 BOSS 直聘的岗位列表和 JD（岗位描述）抓下来，合并去重成一张干净主表，
并导出 Excel。整套流程按"**能中断、能续跑、不丢数据**"设计。

## 适用场景

- 按关键词（如「AI产品经理」）批量抓岗位列表 + JD
- 主表里 JD 缺一大片，需要**补抓**
- 多个采集产物 / 多条流水线的 CSV 需要**按 job_id 合并去重**
- 正在跑的流水线**卡住了 / 想重启 / 想查进度**

## 目录结构

```
boss-jd-pipeline/
├── SKILL.md
├── requirements.txt
├── scripts/
│   ├── config.py            ★ 统一配置层（所有路径/端口/账号都在这，env 可覆盖）
│   ├── inject_cookies.py      注入 Cookie + 拉起常驻 CDP 浏览器
│   ├── collect_matrix.py      关键词×城市 矩阵采集（列表，快）
│   ├── collect_cities.py      多城市「列表+JD」采集（逐城落盘，可续跑）
│   ├── collect_city.py        单城市 分批+冷却（依赖 boss-agent CLI）
│   ├── collect2.py            单城市 先取链接再采集（依赖 boss-agent CLI）
│   ├── backfill_jd.py       ★ JD 补抓核心（分块/续跑/双向/容错）
│   ├── start_both.py        ★ 双泳道守护启动器（幂等，日常就用它）
│   ├── start_backfill.py      单泳道守护启动器
│   ├── finalize_jd.py       ★ 收尾合并（含时间戳产物）→ 主表 + Excel
│   ├── merge_all.py           原始 CSV → 列表主表（按 job_id 去重）
│   ├── merge_city_csvs.py     多城市 CSV 合并去重（有 JD 的优先）
│   ├── stat_progress.py     ★ 进度统计（并集口径，只读）
│   ├── analyze_jobs.py        生成 Markdown 市场分析报告（薪资/技能/城市分布）
│   ├── netscape_to_json.py    Netscape cookie → JSON
│   ├── kv_to_json.py          name=value cookie → JSON
│   └── diagnose/              排查用：进程、风控、代理、详情页
├── scraper/                ★ 上游抓取内核（eatmoreduck/boss-zhipin-scraper, MIT）
│   ├── boss_cdp_raw.py
│   ├── job_summary.py
│   ├── requirements.txt
│   └── LICENSE.upstream-MIT
└── references/
    ├── workflow.md         端到端流程与推荐节奏
    └── pitfalls.md         ★ 踩坑清单（务必先读，能省几小时）
```

## 依赖

```bash
pip install -r requirements.txt
```

`requirements.txt` 内容：`patchright`（浏览器自动化）、`psutil`（进程/端口）、
`websocket-client`（CDP）、`openpyxl`（Excel 导出）、`requests`。

> `collect_city.py` / `collect2.py` 额外需要 `boss-agent` CLI（pip 包 `boss_agent_cli`，
> 提供 `boss.exe`）。**日常补抓不需要它们**，用 `collect_matrix.py` + `backfill_jd.py` 即可。

## 配置

**所有路径集中在 `scripts/config.py`，并且每一项都能用环境变量覆盖**，不用改代码：

| 环境变量 | 作用 | 默认 |
|---|---|---|
| `BOSS_BASE` | 工作区根目录 | 见下方「BASE 如何解析」 |
| `BOSS_DATA_DIR` | 数据目录 | `<BASE>/岗位数据` |
| `BOSS_KEYWORD` | 关键词 | `AI产品经理` |
| `BOSS_DATASET` | 数据集名 | `AI产品经理_全量数据` |
| `BOSS_PY` | 子进程用的解释器 | 自动探测（见下） |
| `BOSS_PROXY` | 代理，如 `socks5://127.0.0.1:1080` | 读 `.boss-agent/proxy.txt` |
| `BOSS_BROWSER` | `chrome` / `edge` | `chrome` |
| `BOSS_CDP_PORT` | 浏览器调试端口 | `9222` |
| `BOSS_PROFILE` | 浏览器 profile 目录 | `<BASE>/.boss-agent/cdp-profile` |

### BASE 如何解析

`config._default_base()` 依次回退，任何一步命中就返回：

1. **`BOSS_BASE` 环境变量** —— 推荐，换机器/换项目只需设这一个
2. **skill 目录下的 `.boss-base` 文件**（单行路径）—— 本地已经固定好、不想每次敲环境变量的场景；
   该文件已进 `.gitignore`，不会被提交
3. **当前工作目录** —— 兜底。此时数据目录默认落在 `<cwd>/岗位数据`，所以请在项目根目录下运行

### 解释器如何探测

`config._pick_python()` 决定父进程用哪个 Python 去拉起子进程，依次尝试：
`BOSS_PY` 环境变量 → 工作区 `.venv` / `venv` → WorkBuddy 隔离环境 `boss-agent`
→ 当前解释器同目录 → 当前解释器。

**为什么不能直接用 `sys.executable`**：子进程需要 `patchright` / `psutil` /
`websocket-client` / `openpyxl`，如果当前解释器没装，子进程会直接崩。
定时任务里建议显式设 `BOSS_PY`。

---

## 标准流程

### 0. 准备 Cookie

从浏览器用 Cookie-Editor 导出 zhipin 的 JSON，或让脚本转换：

```bash
python scripts/netscape_to_json.py cookies.txt cookies.json   # Netscape 格式
python scripts/kv_to_json.py       cookies.txt cookies.json   # name=value 格式
```

存到 `<BASE>/.boss-agent/cookies_new.json`（泳道 A）/ `cookies_liyoutian.json`（泳道 B）。

### 1. 拉起浏览器（每个账号一个）

```bash
python scripts/inject_cookies.py <cookie.json> <profile目录>
```

脚本会：杀掉**它自己那个**浏览器（不误伤另一个）→ 用独立 profile 拉起
→ 注入 Cookie → 检测出口 IP → 打开搜索页验证登录态
（**判据：页面里能看到明文薪资 `15-25K` 这类样本**）。

两个账号要**不同浏览器 + 不同 CDP 端口 + 不同 profile**：

```bash
BOSS_BROWSER=chrome BOSS_CDP_PORT=9222 BOSS_PROFILE=.../cdp-profile     python scripts/inject_cookies.py cookies_new.json
BOSS_BROWSER=edge   BOSS_CDP_PORT=9223 BOSS_PROFILE=.../edge-profile    python scripts/inject_cookies.py cookies_liyoutian.json
```

### 2. 采集岗位列表

```bash
python scripts/collect_matrix.py 10 25 10000      # 每轮10页, 轮间隔25s, 目标1万条
```

关键词×城市矩阵（14 词 × 5 城）突破单查询 ~150-300 条的限制。
按 job_id 全局去重、增量落盘、命中风控自动冷却、`_done.json` 支持续跑。

### 3. 合并成列表主表

```bash
python scripts/merge_all.py     # → <DATA_DIR>/<DATASET>.csv，按 job_id 去重
```

### 4. 补抓 JD（核心）

日常只需要一条命令，**幂等，可以反复执行**：

```bash
python scripts/start_both.py 120
```

它做三件事：

1. 读两张主表算进度，**已完成就直接退出**
2. 检查两个 CDP 端口是否在线，不在线就用各自 Cookie 拉起
3. 某条泳道**没在跑**时才启动它（已在跑就不动，避免打断进行中的块）

两条泳道：

| 泳道 | 浏览器 | CDP | 实例锁 | 方向 | 输出 |
|---|---|---|---|---|---|
| Chrome/forward | Chrome | 9222 | 9333 | 从前往后 | `<DATASET>_含JD.csv` |
| Edge/reverse | Edge | 9223 | 9335 | 从后往前 | `<DATASET>_含JD_edge.csv` |

**方向相反 + 数据源相同 ⇒ 天然不重复抓同一批岗位**，两条流水线并行提速。

### 5. 看进度

```bash
python scripts/stat_progress.py --verbose
```

输出 `DONE=2411/2445 (98.6%) missing=34`、forward/reverse 拆分、在途新增、越界数，
并顺带扫两条日志里的风控关键词。

### 6. 收尾合并 + 导出 Excel

```bash
python scripts/finalize_jd.py
```

吸收 **edge 族主表 + 所有带时间戳的中间 CSV + 两个工作目录下的 `out_*.json`**，
回写主表并生成 `.xlsx`（含冻结首行、自动列宽、控制字符清洗）。

### 7. 生成市场分析报告（可选）

```bash
python scripts/analyze_jobs.py
```

读合并后的主表，按 `job_id` 统计薪资分布 / 技能词频 / 城市分布，输出 Markdown 报告。

---

## 进度统计口径（重要）

**不要拿「主表非空条数」当进度** —— 会严重低估。原因有两个：

1. 并发写冲突时，`save()` 会**退写成带时间戳的文件**（`..._含JD_1789660388.csv`），
   主表停留在旧版本，最新数据散落在多个时间戳文件里；
2. 分块产出的 `out_*.json` 要到**块结束**才合并进主表，块跑到一半时主表不动是正常的。

正确口径（`stat_progress.py` 已实现）：

```
真实进度 = 全部 <DATASET>_含JD*.csv 的并集 ∪ 两泳道在途 out_*.json 里"不在并集内"的 job_id
forward  = forward 族 CSV ∪ forward 在途
reverse  = (edge 族 CSV ∪ edge 在途) − forward      ← 必须取"独有增量"
```

`reverse` 之所以要减 `forward`：edge 表是从主表**复制**起步的（两边都是 2445 行），
直接相加会把同一批岗位数两遍。

---

## 风控与合规

BOSS 对高频详情页抓取有风控。这套流水线已经内置的应对：

- **软风控识别**：`backfill_jd.py` 检测 `访问受限 / 暂时无法访问 / 异常行为 / 安全验证 /
  code 37 / code 31`，命中就暂停 15 分钟；若确认「访问受限」则**立即停本轮等待解封**
- **降速节奏**：每块之间休息 `BLOCK_REST`（默认 240s），块大小默认 120 条
- **启动器护栏**：`restricted()` 发现标签页大面积是 `/passport/` 或 `403.html` 就**跳过不启动**，
  避免无效重试加重风控
- **双账号分摊**：两条泳道并行，单账号速率减半，触发概率明显降低

出问题时：

- **软风控（安全验证页）**：在浏览器窗口**人工完成滑块验证**即可恢复，不必等解封时刻。
  参考 `references/pitfalls.md` 用 `probe_block_scope.py` 区分「IP 级」还是「账号级」。
- **硬封（访问受限 + 显示恢复时刻）**：等解封，别重试。

> ⚠️ 请遵守目标站点的 robots / 服务条款与当地法律；仅抓取你有权访问的公开数据，
> 控制频率，不要用于批量转售或侵犯他人隐私。

---

## 常用排查命令

```bash
python scripts/stat_progress.py --verbose         # 进度 + 风控扫描（只读，最常用）
python scripts/diagnose/list_workers.py           # 列出 backfill / scraper 进程及运行时长
python scripts/diagnose/cleanup_workers.py        # 清理这些进程
python scripts/diagnose/probe_block_scope.py      # 匿名 vs 已登录 对照，判断封禁层级
python scripts/diagnose/probe_threshold.py 北京 15 5   # 探测翻页到第几页触发风控
python scripts/diagnose/probe_proxy.py            # 扫描本机可用代理端口
python scripts/diagnose/check_detail_page.py <职位URL>  # 打开详情页看拿到什么
```

Windows 下查端口属主（实例锁就是端口）：

```bash
netstat -ano | grep -E ":9333|:9335|:9222|:9223"
```

## 关键设计（改代码前先看）

- **实例锁用 TCP 端口，不用锁文件**：本机安全策略会让 `os.remove()` 失败、锁文件删不掉。
  端口由 OS 管理，进程退出自动释放。Windows 必须加 `SO_EXCLUSIVEADDRUSE`，否则可重复绑定、锁失效。
- **所有共享文件名带 pid**：`in_<块号>_<pid>.json` / `out_<块号>_<pid>.json` /
  `_jd_done_<方向>_<端口>.json`。同机并发不可控，文件名一共享就可能 `PermissionError` 崩主循环。
- **主循环有兜底**：`while True: try: main() except: sleep(10)`，非关键路径的异常绝不终止抓取。
- **恢复池**：启动时 `absorb_existing()` 扫 `out_*.json`，把已抓到的 JD 收回来，所以被杀/崩溃**不丢数据**。
- **认领文件**：`claim_<块号>.lock` 记录 pid，并发实例各做不同块，避免重复抓同一批。

更多细节见 `references/pitfalls.md`。
