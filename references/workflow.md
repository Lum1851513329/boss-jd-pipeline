# 端到端流程与推荐节奏

## 全景

```
                 ┌──────────────────────────────────────────────┐
                 │  0. 准备 Cookie  →  inject_cookies.py        │
                 │     泳道A: Chrome 9222 / 泳道B: Edge 9223     │
                 └───────────────────┬──────────────────────────┘
                                     ▼
        ┌────────────────────────────────────────────────────────┐
        │  1. 采集列表  collect_matrix.py                        │
        │     14 关键词 × 5 城市，按 job_id 去重，增量落盘        │
        └───────────────────┬────────────────────────────────────┘
                            ▼
        ┌────────────────────────────────────────────────────────┐
        │  2. 合并列表主表  merge_all.py                          │
        │     → <DATASET>.csv（无 JD）                            │
        └───────────────────┬────────────────────────────────────┘
                            ▼
        ┌────────────────────────────────────────────────────────┐
        │  3. 补抓 JD  start_both.py 120   ← 反复执行，幂等       │
        │     forward 泳道 ──┐                                    │
        │     reverse 泳道 ──┴─ 并行，方向相反，不重叠            │
        └───────────────────┬────────────────────────────────────┘
                            ▼
        ┌────────────────────────────────────────────────────────┐
        │  4. 看进度  stat_progress.py                            │
        └───────────────────┬────────────────────────────────────┘
                            ▼
        ┌────────────────────────────────────────────────────────┐
        │  5. 收尾合并  finalize_jd.py → 主表 + .xlsx             │
        └────────────────────────────────────────────────────────┘
```

## 命令序列

```bash
export SKILL=~/.workbuddy/skills/boss-jd-pipeline/scripts
PY="C:/Users/li/.workbuddy/binaries/python/envs/boss-agent/Scripts/python.exe"

# 0. 注入 Cookie（两个账号各一次）
"$PY" "$SKILL/inject_cookies.py" <BASE>/.boss-agent/cookies_new.json
BOSS_BROWSER=edge BOSS_CDP_PORT=9223 BOSS_PROFILE=<BASE>/.boss-agent/edge-profile \
  "$PY" "$SKILL/inject_cookies.py" <BASE>/.boss-agent/cookies_liyoutian.json

# 1-2. 采集列表并合并
"$PY" "$SKILL/collect_matrix.py" 10 25 10000
"$PY" "$SKILL/merge_all.py"

# 3. 补抓 JD —— 反复执行即可，幂等
"$PY" "$SKILL/start_both.py" 120

# 4. 看进度
"$PY" "$SKILL/stat_progress.py" --verbose

# 5. 收尾
"$PY" "$SKILL/finalize_jd.py"
```

## 推荐节奏

| 参数 | 建议值 | 理由 |
|---|---|---|
| 块大小 `chunk` | **60~120** | 120 时单块 10~60 分钟；降到 60 减少被定时任务打断的损失 |
| 块间休息 `BLOCK_REST` | **240s** | 稳健优先，显著降低风控概率 |
| 定时触发周期 | **2 小时**（而非 1 小时） | 单块耗时与 1 小时同量级，1 小时会反复打断进行中的块 |
| 矩阵采集轮间隔 | 25s | `collect_matrix.py` 默认 |
| 单城市冷却 | 480s | `collect_city.py` 默认 |

### 关于"被打断"

这套设计对打断是**安全**的：

- 已抓的 JD 会在下次启动时被 `absorb_existing()` 从 `out_*.json` 收回（恢复池）
- 分块产物按块号+pid 命名，不会互相覆盖
- 认领文件保证并发实例各做不同块

代价只是**吞吐变慢**（重复劳动 + 频繁重启开销）。

> 观察到的真实问题：`forward` 泳道会**重复抓 `reverse` 已经拿到的岗位**，
> 因为两边各自只比较"自己源表有没有 JD"。如果提速需求强烈，
> 可以让 `backfill_jd.py` 的 `todo` 过滤掉"另一条泳道已有 JD"的 job_id ——
> 这需要在合并时先算一次全局并集。

## 产物清单

| 文件 | 说明 |
|---|---|
| `<DATASET>.csv` | 岗位列表主表（可能无 JD） |
| `<DATASET>_含JD.csv` | forward 泳道主表 |
| `<DATASET>_含JD_edge.csv` | reverse 泳道主表 |
| `<DATASET>_含JD_<ts>.csv` | 写冲突时的时间戳退写产物（**必须纳入合并**） |
| `<DATASET>_含JD.xlsx` | 最终 Excel |
| `_JD补抓/` `_JD补抓_edge/` | 工作目录：`in_*.json` 输入、`out_*.json` 产出、`claim_*.lock` 认领、`backfill*.log` 日志 |
| `_JD补抓/_jd_done_<方向>_<端口>.json` | 进度文件（也是恢复池） |

## 表结构（17 列）

```
job_id, 岗位名称, 所在城市, 区域, 薪资范围, 公司名称, 岗位描述,
工作经验要求, 学历要求, 岗位链接, 公司规模, 融资阶段, 所属行业,
技能标签, 福利待遇, Boss活跃度, 薪资来源
```

- 合并/去重一律**以 `job_id` 为主键**
- 合并时**有 JD 的优先**（`merge_city_csvs.py`）或**只填空不覆盖**（`finalize_jd.py`）

## 多城市扩展

```bash
# 每个城市一条流水线，各写各的输出 CSV（文件名要互不相同）
"$PY" "$SKILL/collect_cities.py" out_广州.csv 9222 10 广州 成都 &
"$PY" "$SKILL/collect_cities.py" out_南京.csv 9223 10 南京 长沙 &

# 最后合并去重
"$PY" "$SKILL/merge_city_csvs.py"
```

`collect_cities.py` 逐城落盘、可续跑（详情已抓够 ≥85% 就复用中间文件），
所以断了直接重跑同一条命令即可。
