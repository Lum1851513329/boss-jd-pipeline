"""多城市「列表 + JD」一次性采集（可续跑、逐城落盘）。

流水线位置
==========
属于**采集列表 + JD**阶段：一次调用就把列表和详情都抓好（scraper 加 `--detail`），
产出可以直接进后续清洗/导出，不用再单独补 JD。

用法：
  python collect_cities.py <输出CSV> <CDP端口> <页数> <城市1> [<城市2> ...]

  * 输出CSV   本实例的结果表（多实例各写各的，最后用 merge_city_csvs.py 合并）
  * CDP端口   已有带界面已登录浏览器的调试端口（脚本会接管它，不自己起新浏览器）
  * 页数      每个城市翻多少页
  * 城市1..N  依次采集，遇到风控就停下（后面的城市留给下次续跑）

设计要点（针对本机中断频繁的问题）：
  * **每个城市跑完立刻写一次 CSV**，轮次被中断也不会丢已完成的成果
  * **可续跑**：启动时先看中间文件，详情已抓够（≥列表量的 85%）就复用，不重复抓
  * 中间文件名带 pid，避免并发抢写
  * 多实例并行时各写各的输出 CSV，最后用 merge_city_csvs.py 合并

关键设计决策
------------
  * 必须复用**带界面的、已登录的**浏览器（走 CDP 接管）：BOSS 会对无头 /
    独立 profile 的浏览器做风控，自己新起一个干净浏览器基本拿不到数据。
  * 逐城落盘 + 85% 复用阈值，是为了让「跑一半被打断」几乎不损失已有成果。
  * 中间文件名带 `<端口>_<pid>` 后缀，两个实例同时跑同一城市时不会互相覆盖。

重要副作用
----------
  * 在 WORK 目录（默认 `<DATA_DIR>/_新6城`）下写 `list_<城市>_<tag>.json` 和
    `detail_<城市>_<tag>.json` 两个中间文件。
  * 向北 `out_csv` 增量合并写入（按 job_id 去重）。
  * 通过 subprocess 调用上游 scraper 抓取，浏览器由 scraper 经 CDP 接管。

环境变量
--------
  BOSS_CITY_WORK  覆盖 WORK 目录
  BOSS_FORCE=1    忽略复用，强制重抓
"""

import csv
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import DATA_DIR, FIELDS, KEYWORD, PY, SCRAPER  # noqa: E402

# 中间文件目录：可用 BOSS_CITY_WORK 覆盖，方便把不同批次的数据分开存
WORK = os.environ.get("BOSS_CITY_WORK") or os.path.join(DATA_DIR, "_新6城")


def log(m):
    """带时间戳打印一行进度日志。

    参数 m: 要打印的文本。
    flush=True 让日志在被重定向/管道时也能实时输出。
    """
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def split_loc(loc):
    """把 '北京·朝阳区·左家庄' 拆成 (城市, 区域)。

    与 collect_matrix.split_location 等价，只是实现更紧凑：第一段是城市，
    其余段拼回区域。空值返回两个空串。

    参数 loc: 原始位置字符串，可能为 None。
    返回: (城市, 区域)。
    """
    parts = (loc or "").split("·")
    return (parts[0].strip() if parts else "",
            "·".join(p.strip() for p in parts[1:] if p.strip()))


def split_tags(tags):
    """把 '3-5年 | 本科' 这类 tags 按 | 拆成 (经验, 学历)。

    这里走的是**按位置**的简化版（第一段经验、第二段学历），
    不像 collect_matrix.split_tags 那样做内容识别——因为本文件调用的
    scraper 输出里这两段位置稳定。

    参数 tags: 原始 tags 字符串，可能为 None。
    返回: (第一段, 第二段)，缺省为空串。
    """
    t = (tags or "").split("|")
    return (t[0].strip() if t else "", t[1].strip() if len(t) > 1 else "")


def load_json(path, default):
    """读一个 JSON 文件，读不到/解析失败时返回 default。

    参数 path: 文件路径；为 None 表示「这个城市还没有任何中间文件」。
    参数 default: 兜底返回值（列表或字典，取决于调用处期望的结构）。
    读取失败只打一行日志、不抛异常——中间文件损坏不该让整批采集失败。
    """
    if not path:  # 新城市还没有任何中间文件
        return default
    try:
        if os.path.exists(path):
            return json.load(open(path, encoding="utf-8"))
    except Exception as exc:
        log(f"  读取 {os.path.basename(path)} 失败：{str(exc)[:70]}")
    return default


def find_existing(city):
    """找该城市已有的列表/详情文件（任意 pid 版本，取最大的）。

    「最大的」是内容最全的近似判断——中间文件是追加/整体重写的，
    正常情况下更大的文件包含更多岗位。注意这只是启发式：
    如果某个旧版本恰好体积更大（比如字段更长），可能选到偏旧的那份。

    参数 city: 城市名。
    返回: (列表文件路径或 None, 详情文件路径或 None)。
    """
    import glob
    lst = sorted(glob.glob(os.path.join(WORK, f"list_{city}_*.json")), key=os.path.getsize, reverse=True)
    det = sorted(glob.glob(os.path.join(WORK, f"detail_{city}_*.json")), key=os.path.getsize, reverse=True)
    return (lst[0] if lst else None), (det[0] if det else None)


def build_rows(city, lst_path, det_path):
    """把某个城市的列表 JSON + 详情 JSON 合成待写入 CSV 的行。

    参数:
      city     : 城市名（列表里 location 缺失时用它兜底）
      lst_path : 列表 JSON 路径（可能为 None）
      det_path : 详情 JSON 路径（可能为 None）

    返回: (rows, 列表条数, 带 JD 的条数)

    副作用: 无（只读文件）。
    """
    data = load_json(lst_path, {})
    # 兼容两种列表格式：{"jobs": [...]} 或直接就是一个数组
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    # 详情单独存成 [{job_id, jd}, ...]，先建成 job_id -> jd 的映射再回填
    jd_map = {}
    for d in load_json(det_path, []):
        j = str((d or {}).get("jd") or "").strip()
        if j:
            jd_map[d.get("job_id")] = j
    rows = []
    for x in jobs:
        cn, area = split_loc(x.get("location", ""))
        exp, deg = split_tags(x.get("tags", ""))
        rows.append({
            "job_id": x.get("job_id", ""), "岗位名称": x.get("title", ""),
            "所在城市": cn or city, "区域": area, "薪资范围": x.get("salary", ""),
            "公司名称": x.get("boss_name", ""), "岗位描述": jd_map.get(x.get("job_id"), ""),
            "工作经验要求": exp, "学历要求": deg, "岗位链接": x.get("job_link", ""),
            "公司规模": x.get("company_scale", ""), "融资阶段": x.get("company_stage", ""),
            "所属行业": x.get("company_industry", ""),
            # 技能标签字段改过名，两个 key 都试一下
            "技能标签": x.get("skills", "") or x.get("job_labels", ""),
            "福利待遇": x.get("welfare", ""), "Boss活跃度": x.get("boss_active_status", ""),
            "薪资来源": x.get("salary_source", ""),
        })
    return rows, len(jobs), len(jd_map)


def save_merge(out_csv, rows):
    """把 rows 并入 out_csv（按 job_id 去重，新数据覆盖旧数据）。

    参数:
      out_csv : 目标 CSV 路径（没有则新建）
      rows    : build_rows 产出的行列表

    返回: 合并后 out_csv 的总行数。

    副作用: 写 out_csv。先写带 pid 的 tmp 再 os.replace 原子替换；
    Windows 上 CSV 被 Excel 打开会导致 replace 失败（PermissionError），
    因此重试 5 次、每次退避 1~5 秒，尽量把数据写进去。
    """
    existing = {}
    if os.path.exists(out_csv):
        for r in csv.DictReader(open(out_csv, encoding="utf-8-sig")):
            existing[r.get("job_id")] = r
    for r in rows:
        # 没有 job_id 的行不进字典（也无法去重）；有 job_id 的新行覆盖旧行
        if r["job_id"]:
            existing[r["job_id"]] = r
    tmp = f"{out_csv}.{os.getpid()}.tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        # extrasaction="ignore"：行里多余的 key 直接丢弃，不报错
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in existing.values():
            w.writerow(r)
    for i in range(5):
        try:
            os.replace(tmp, out_csv)
            break
        except PermissionError:
            time.sleep(1 + i)
    return len(existing)


def process_city(city, pages, port, tag, out_csv, force=False):
    """处理单个城市：决定是复用已有成果还是重新采集，然后把结果并入 out_csv。

    参数:
      city    : 城市名
      pages   : 翻开多少页
      port    : CDP 端口（交给 scraper 的 --cdp-port）
      tag     : 本次运行的文件名后缀，形如 `<端口>_<pid>`
      out_csv : 目标 CSV
      force   : True 则跳过复用判断，强制重抓

    返回:
      None  正常完成（含「复用成功」）——调用方继续下一城
      字符串 命中风控时的日志尾部——调用方据此停止本轮
      （注意：子进程异常时也返回 None，即异常不会中断整批，见下方注释）

    副作用: 可能拉起 scraper 子进程；写中间 JSON；写 out_csv。
    """
    lst_path = os.path.join(WORK, f"list_{city}_{tag}.json")
    det_path = os.path.join(WORK, f"detail_{city}_{tag}.json")

    old_l, old_d = find_existing(city)
    reuse_l, reuse_d = old_l, old_d
    need_run = force
    if not need_run:
        rows, n_list, n_jd = build_rows(city, reuse_l, reuse_d)
        if n_list == 0:
            # 完全没有列表数据，必须真跑一次
            need_run = True
        elif n_jd < max(int(n_list * 0.85), 1):
            # 详情覆盖率不到 85%，认为上次被打断在 JD 阶段，重抓补齐
            log(f"  {city} 详情不足（{n_jd}/{n_list}），需要续抓")
            need_run = True
        else:
            log(f"  {city} 复用已有成果：列表 {n_list} / 带 JD {n_jd}")
    if need_run:
        log(f"=== {city} 开抓（{pages} 页，CDP {port}）===")
        # --max-details 按「每页约 15 条」估算 JD 数上限，再加 10 条余量
        cmd = [PY, SCRAPER, "--keyword", KEYWORD, "--city", city,
               "--pages", str(pages), "--detail",
               "--max-details", str(pages * 15 + 10), "--cdp-port", str(port),
               "--output", lst_path, "--detail-output", det_path]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=10800,
                               env=dict(os.environ, PYTHONUTF8="1"))
            tail = ((p.stdout or "") + (p.stderr or ""))[-300:]
        except Exception as exc:
            log(f"  {city} 执行异常：{str(exc)[:120]}")
            # 返回 None 表示「非风控的失败」：跳过本城继续下一城，不中断整批
            return None
        # 风控关键词命中即认为该城被拦，返回尾部让 main 停下来休息
        if any(k in tail for k in ("访问受限", "暂时无法访问", "异常行为", "安全验证")):
            log(f"  ⚠️ {city} 疑似风控，停止本轮。尾部：{tail[-160:]}")
            return tail
        # 本次真正跑过，后续复用这一轮新落盘的中间文件
        reuse_l, reuse_d = lst_path, det_path

    rows, n_list, n_jd = build_rows(city, reuse_l, reuse_d)
    total = save_merge(out_csv, rows)
    log(f"  ✅ {city} 完成：列表 {n_list} / 带 JD {n_jd} → 累计 {total} 条")
    return None


def main():
    """入口：解析命令行参数，逐城采集，遇到风控就停。

    必填参数: argv[1]=输出CSV, argv[2]=CDP端口, argv[3]=页数,
    argv[4:]=城市列表（至少一个）。

    副作用: 创建 WORK 目录、逐城写 CSV/中间 JSON、城间 sleep 5 秒降温。
    """
    out_csv, port, pages = sys.argv[1], sys.argv[2], int(sys.argv[3])
    cities = sys.argv[4:]
    # BOSS_FORCE=1 时忽略复用判断，全部重抓（排查数据问题时用）
    force = os.environ.get("BOSS_FORCE", "") == "1"
    # 后缀带上端口和 pid：多实例并行时同一城市的中间文件互不覆盖
    tag = f"{port}_{os.getpid()}"
    os.makedirs(WORK, exist_ok=True)

    for c in cities:
        why = process_city(c, pages, port, tag, out_csv, force)
        if why:
            # 风控：后面的城市留给下次续跑，避免把账号往死里撞
            break
        time.sleep(5)
    log(f"本轮结束 → {out_csv}")


if __name__ == "__main__":
    main()
