# 变更日志

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 风格，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.0.0] — 2026-10-08

首次开源发布。项目在私有环境下经过完整一轮「2445 条岗位」的真实采集打磨后整理发布。

### 新增

**采集**
- `collect_matrix.py` — 关键词 × 城市矩阵采集，按 `job_id` 全局去重、增量落盘、
  `_done.json` 支持续跑、命中风控自动冷却
- `collect_cities.py` — 多城市「列表 + JD」一次性采集，逐城落盘、可续跑
  （详情已抓够 ≥ 85% 则复用中间文件）
- `collect_city.py` / `collect2.py` — 单城市分批+冷却采集（依赖 `boss-agent` CLI）

**JD 补抓**
- `backfill_jd.py` — 分块补抓核心：**恢复池**（启动时回收 `out_*.json` 已抓成果）、
  每块立即落盘、双向分工（`forward` / `reverse` 由环境变量驱动）、
  认领文件避免并发重复抓、主循环兜底
- `start_both.py` — 双泳道守护启动器，幂等：算进度 → 保浏览器在线 → 只启动没在跑的泳道
- `start_backfill.py` — 单泳道守护启动器

**合并去重**
- `finalize_jd.py` — 收尾合并：吸收 edge 族主表 + **所有带时间戳的中间 CSV** +
  两个工作目录下的 `out_*.json`，回写主表并导出 Excel
- `merge_all.py` — 原始采集 CSV → 列表主表（按 `job_id` 去重）
- `merge_city_csvs.py` — 多城市 CSV 合并去重（有 JD 的优先）

**统计与分析**
- `stat_progress.py` — 并集口径进度统计 + 风控关键词扫描（只读）
- `analyze_jobs.py` — 薪资分布 / 技能词频 / 城市分布的 Markdown 报告

**工具与诊断**
- `config.py` — 统一配置层，所有路径/端口/账号集中一处且可用环境变量覆盖
- `inject_cookies.py` — 注入 Cookie + 拉起常驻 CDP 浏览器 + 验证登录态
- `netscape_to_json.py` / `kv_to_json.py` — cookie 格式转换
- `diagnose/` — 进程排查、风控层级判定、翻页阈值探测、代理探测、详情页取证

### 修复

以下都是**真实跑出来的问题**，不是理论推演：

- **`inject_cookies.py` 误杀用户浏览器** — 原实现按进程名杀掉**所有** `chrome.exe` /
  `msedge.exe`，会把用户自己开着的个人浏览器一起关掉、丢失未保存标签页。
  现改为按 `--user-data-dir` 精确匹配本流水线的 profile，**匹配不到就什么都不杀**。
- **`collect2.py` 同类问题** — 同样改为按 profile 精确匹配。
- **`netscape_to_json.py` 丢失 httpOnly 登录态 cookie** — Netscape 规范的扩展约定中，
  httpOnly cookie 行以 `#HttpOnly_` 开头，原实现把这些行当注释丢掉了；
  而登录态关键字段 `__zp_stoken__` 往往正是 httpOnly 的，会导致「注入成功但仍未登录」。
  现已正确解析前缀并把 `httpOnly` 写入输出。
- **`finalize_jd.py` 漏合并 forward 族时间戳产物** — 原实现只 glob `edge*`，
  但并发写冲突时 **forward 族**同样会退写成带时间戳的文件。
  这个缺口曾导致 **313 条 JD** 落在中间文件里没并进主表。
- **`merge_all.py` / `collect_matrix.py` 写盘不安全** — tmp 文件名固定（并发会互相覆盖）
  且 `os.replace` 无重试（Windows 上主表被 Excel 占用就直接抛异常中断整轮）。
  现改为 tmp 带 pid + 重试 5 次。
- **`collect_matrix.py` 风控关键词不全** — 手写的精简列表漏了
  `访问受限` / `暂时无法访问` / `异常行为`，命中时不会冷却、继续硬跑、把风控加重。
  现统一取自 `config.BLOCK_KEYWORDS`。
- **`merge_city_csvs.py` 文档与实现不符** — 文档提到 `--append-to` 参数但并未实现，已修正文档。

### 已知限制

- **仅支持 Windows**：依赖 `creationflags` 脱离会话、`SO_EXCLUSIVEADDRUSE` 等
  Windows 特有行为。移植到 macOS/Linux 需重写进程与端口相关代码。
- **`forward` 泳道存在重复劳动**：两条泳道各自只比较「自己源表有没有 JD」，
  因此 `forward` 可能会重抓 `reverse` 已经拿到的岗位。若需提速，
  可在 `backfill_jd.py` 的 `todo` 计算时先取一次全局并集过滤。
- **块耗时与定时周期同量级**：块大小 120 时单块 10~60 分钟，与 1 小时级的定时任务
  同量级，每次触发都会打断进行中的块。**推荐把块大小降到 60，或把定时周期改成 2 小时**
  （打断本身不丢数据，只是降低吞吐）。
- **`backfill_jd.py` 中有一段死代码**：`claim_batch` 里 `import atexit` 之后的清理块
  位于所有 `return` 之后，永不执行，且引用的是已废弃的锁文件变量。已用 `# TODO` 标注，
  未删除以保持与线上版本一致。

### 设计说明（为什么这么做）

- **实例锁用 TCP 端口而不是锁文件** —— 本机安全策略会让 `os.remove()` 失败，
  锁文件删不掉会永久残留；TCP 端口由 OS 管理，进程退出自动释放。
  Windows 必须显式设 `SO_EXCLUSIVEADDRUSE`，否则默认允许重复绑定、锁形同虚设。
- **所有共享文件名带 pid** —— 同类 `PermissionError` 在本项目出现过三次
  （`out_*.json` → `_jd_done_*.json` → `in_*.json`），第三次才补全。
- **进度统计必须用并集口径** —— 只看主表会严重低估（曾低估 545 条）。
- **合并策略刻意分两种** —— `finalize_jd.py` / `merge_all.py` 是「只填空、不覆盖已有」
  （怕低质量覆盖高质量）；`merge_city_csvs.py` 是「新数据覆盖旧数据、但有 JD 的优先」。
- **block 间休息 240s** —— 稳健优先，显著降低风控概率。

---

## 尚未发布的规划

- [ ] 让 `forward` 泳道跳过 `reverse` 已覆盖的岗位（消除重复劳动）
- [ ] 跨平台支持（macOS / Linux）
- [ ] 把 `config.py` 拆出「人可读的 `config.yaml`」层，环境变量作为覆盖
- [ ] 为合并/统计逻辑补单元测试（目前只有端到端手工验证）
- [ ] 清理 `backfill_jd.py` 中 `claim_batch` 的死代码块
