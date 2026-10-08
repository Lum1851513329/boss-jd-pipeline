"""BOSS 直聘「AI 产品经理」岗位批量采集器（方案2 · CDP 旁听模式）。

流水线位置
==========
本脚本属于**采集列表**阶段：只抓列表字段（`--no-detail`），**不抓 JD**。
JD 由后续的补抓脚本按需补，这样可以先把覆盖面铺开、把量做上去，
再决定给哪些岗位花时间去抓详情页。

设计要点：
  * 用 eatmoreduck/boss-zhipin-scraper（旁听页面自身请求，风控最轻）
  * 「关键词 × 城市」矩阵，突破单查询 ~150-300 条的上限
  * 按 job_id 全局去重，增量落盘（可随时中断/续跑）
  * 每轮之间有自然等待；命中风控自动冷却重试
  * 先只取列表字段（快），JD 后续按需补

为什么要做「关键词 × 城市」矩阵
-------------------------------
BOSS 对**单个查询**（一个关键词 + 一个城市）大约只放 150~300 条结果，
再往后翻也不给新岗位了。换成不同关键词（大模型产品经理 / AIGC 产品经理 /
智能体产品经理 …）或不同城市，就各自还能再拿一批。把所有组合的结果按
job_id 全局去重合并，就能得到一张远超单查询上限的表。

重要副作用
----------
  * 在 `<DATA_DIR>/_方案2测试/matrix_raw/` 下为每个组合落盘一份原始 CSV
    （`<关键词>_<城市>.csv`），并维护进度文件 `_done.json` 供断点续跑。
  * 每采完一轮就把**整张主表**重写到 MASTER（即 config 里的 SRC），
    采用「先写 tmp 再原子替换」，中断不会把原表写坏。
  * 通过 subprocess 调用上游 scraper，浏览器由 scraper 自己接管；
    **本脚本不直接启动浏览器**（所以要先自己把带界面的已登录浏览器开好）。

用法
====
    python collect_matrix.py [每轮页数] [轮间隔秒] [目标条数]

  * 每轮页数  单个组合翻几页，默认 10
  * 轮间隔秒  两次采集之间的自然等待，默认 25
              （命中风控时冷却取 max(gap*4, 300)，即至少 300 秒）
  * 目标条数  主表达到该条数即停止，默认 10000

中断后直接重跑即可：已完成组合记录在 `_done.json`，会自动跳过。
"""

import csv
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import (BLOCK_KEYWORDS, DATA_DIR, FIELDS_LIST, KEYWORD,  # noqa: E402
                    PY, SCRAPER, SRC)

OUTDIR = DATA_DIR
# 原始 CSV + 进度文件都落在这个工作目录下（独立子目录，方便整体清理重来）
RAW = os.path.join(OUTDIR, "_方案2测试", "matrix_raw")
# 主表：复用 config 的「列表主表」路径，即 <DATASET>.csv（不含 JD）
MASTER = SRC

CITIES = ["北京", "上海", "深圳", "杭州", "苏州"]

# 关键词矩阵：围绕主关键词做扩展，突破单查询 ~150-300 条的上限
KEYWORDS = [
    KEYWORD, "大模型产品经理", "智能体产品经理", "AIGC产品经理",
    "AI产品专家", "生成式AI产品经理", "AI应用产品经理", "AI平台产品经理",
    "人工智能产品经理", "AI产品总监", "大模型产品", "AI产品",
    "智能产品经理", "AI解决方案产品经理",
]

# 列表阶段不写「岗位描述」列，所以直接用 config 的列表字段集
FIELDS = FIELDS_LIST


def log(m):
    """带时间戳打印一行进度日志。

    参数 m: 要打印的文本。
    flush=True 是为了日志被重定向到文件时也能实时刷出来，方便 tail 观察。
    """
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def split_tags(tags):
    """把 BOSS 的 tags 拆成 (经验要求, 学历要求)。

    tags 形如 '3-5年 | 本科' 或 '经验不限 | 大专'。

    坑：BOSS 返回的段数和顺序并不固定，不能简单地 parts[0] 当经验、
    parts[1] 当学历。这里先按内容特征识别——含 "年/应届/经验" 的判为经验，
    含 "本科/大专/硕士/博士/中专/高中/初中/学历" 的判为学历；识别不到时
    才退回按位置兜底，尽量不丢字段。

    参数 tags: 原始 tags 字符串，可能为 None。
    返回: (经验, 学历)，识别/兜底都失败则是空字符串。
    """
    parts = [p.strip() for p in str(tags or "").split("|") if p.strip()]
    exp = edu = ""
    for p in parts:
        # 只认第一个命中的，避免后面又冒出一个更短的标签把结果覆盖掉
        if re.search(r"年|应届|经验", p):
            exp = exp or p
        elif re.search(r"本科|大专|硕士|博士|中专|高中|初中|学历", p):
            edu = edu or p
    # 内容识别不出来时的位置兜底：惯例是第一段经验、第二段学历
    if not exp and parts:
        exp = parts[0]
    if not edu and len(parts) > 1:
        edu = parts[1]
    return exp, edu


def split_location(loc):
    """把 '北京·朝阳区·左家庄' 拆成 ('北京', '朝阳区·左家庄')。

    第一个 · 之前是城市名，之后整段原样保留为「区域」（可能还有更细的层级，
    仍用 · 连接）。空值或异常输入统一返回两个空串，省得调用方再做 None 判断。
    """
    parts = [p for p in str(loc or "").split("·") if p]
    if not parts:
        return "", ""
    return parts[0], "·".join(parts[1:])


def load_master():
    """把已有主表读进内存，返回 {job_id: row} 字典，供全局去重用。

    首次运行时主表还不存在；也可能被 Excel 打开占用，或内容损坏。
    这两种情况都吞掉异常、当作空表继续跑——采集本身不该因为读表失败而中断。
    用 utf-8-sig 是因为 Excel 另存的 CSV 常带 BOM。
    """
    rows = {}
    if os.path.exists(MASTER):
        try:
            for r in csv.DictReader(open(MASTER, encoding="utf-8-sig")):
                # 没有 job_id 的行无法参与去重，直接丢掉
                if r.get("job_id"):
                    rows[r["job_id"]] = r
        except Exception as exc:
            log(f"读取主表失败（忽略）: {exc}")
    return rows


def save_master(rows):
    """把 {job_id: row} 全量写回主表，返回写入条数。

    先写临时文件再 os.replace 原子替换：即使中途被 Ctrl-C / 断电，
    原来那份主表也不会被截断成半个文件。注意这里是**全量覆盖写**（不是追加），
    job_id 去重已经在内存里做完了。

    tmp 文件名带 pid：本脚本可能被并发触发，固定名 `.tmp` 会互相覆盖
    （本项目踩过三次的同类坑，见 references/pitfalls.md 第 2 节）。

    替换重试 5 次：Windows 上主表被 Excel / 预览面板占用是常态，
    不重试就会抛 PermissionError 直接中断整轮采集
    （与 collect_cities.py 的 save_merge 行为保持一致）。
    """
    tmp = f"{MASTER}.{os.getpid()}.tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows.values():
            w.writerow(r)
    for attempt in range(5):
        try:
            os.replace(tmp, MASTER)
            break
        except PermissionError:
            log(f"  主表被占用，{1 + attempt}s 后重试（{attempt + 1}/5）")
            time.sleep(1 + attempt)
    else:
        log(f"  ⚠️ 主表写入失败，结果保留在 {tmp}")
    return len(rows)


def run_one(keyword, city, pages, out_path, cooldown):
    """采集单个「关键词 × 城市」组合，返回 (落盘条数, 是否命中风控)。

    参数:
      keyword  : 查询关键词（scraper 的 --keyword）
      city     : 城市名（scraper 的 --city）
      pages    : 翻多少页
      out_path : 该组合原始 CSV 的落盘路径
      cooldown : 命中风控后的冷却秒数，由调用方传 max(gap*4, 300)

    返回:
      (条数, False) 正常拿到结果；条数取自落盘 CSV 的行数。
      (0, True)     连续 3 次疑似风控，放弃该组合，交给调用方决定是否停本轮。

    副作用: 拉起 scraper 子进程（子进程内部接管 CDP 浏览器）、写 out_path，
    以及风控/空结果时阻塞式的 time.sleep。

    注意风控关键词是本地这一份，和 config.BLOCK_KEYWORDS 并不完全一致：
    这里额外看了 "访问频繁"，但少了 "访问受限" / "暂时无法访问" / "异常行为"
    三个，命中这几类时不会触发冷却。
    """
    for attempt in range(3):
        # 强制子进程用 UTF-8 输出，否则 Windows 默认 GBK 会把中文日志解成乱码
        env = dict(os.environ, PYTHONUTF8="1")
        p = subprocess.run(
            [PY, SCRAPER, "--keyword", keyword, "--city", city,
             "--pages", str(pages), "--no-detail", "--format", "csv",
             "--output", out_path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=1800,
        )
        # scraper 把提示打在 stdout/stderr 里，合并起来一起找风控关键词。
        out = (p.stdout or "") + (p.stderr or "")
        # 关键词统一取自 config.BLOCK_KEYWORDS（含 访问受限 / 暂时无法访问 / 异常行为 等），
        # 再补一个"访问频繁"。早期这里手写了一份精简列表，漏了前三个关键词，
        # 导致命中「访问受限」时不会冷却、继续硬跑、把风控加重。
        risk = any(k in out for k in tuple(BLOCK_KEYWORDS) + ("访问频繁",))
        if risk:
            # 风控时什么都不做，冷却后 continue 重试（消耗一次 attempt）
            log(f"  ⚠️ 疑似风控，冷却 {cooldown}s（第 {attempt + 1} 次）")
            time.sleep(cooldown)
            continue
        if os.path.exists(out_path):
            try:
                # 有产出文件就算成功；文件存在但 0 行也按 0 条处理
                n = len(list(csv.DictReader(open(out_path, encoding="utf-8-sig"))))
                return n, False
            except Exception:
                pass
        # 没风控也没结果，多半是偶发超时/空页，等 30s 再试
        log(f"  未产出结果，尾部输出: {out.strip()[-200:]}")
        time.sleep(30)
    return 0, True


def main():
    """主循环：按「关键词 × 城市」矩阵逐个采集，直到达标或连续风控。

    参数全部取自命令行位置参数（都有默认值，什么都不传也能跑）：
      argv[1] pages   每轮页数，默认 10
      argv[2] gap     轮间隔秒，默认 25
      argv[3] target  目标条数，默认 10000

    副作用: 创建 RAW 目录、写各组合原始 CSV、写 _done.json、反复重写主表。
    """
    pages = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    gap = int(sys.argv[2]) if len(sys.argv) > 2 else 25
    target = int(sys.argv[3]) if len(sys.argv) > 3 else 10000
    os.makedirs(RAW, exist_ok=True)

    master = load_master()
    log(f"起始主表: {len(master)} 条")

    # 笛卡尔积：关键词在外层，城市在内层（顺序影响采集先后，不影响结果）
    combos = [(k, c) for k in KEYWORDS for c in CITIES]
    done_file = os.path.join(RAW, "_done.json")
    # 断点续跑：读上次已完成的组合，损坏就当空集重来
    done = set()
    if os.path.exists(done_file):
        try:
            done = set(tuple(x) for x in json.load(open(done_file, encoding="utf-8")))
        except Exception:
            done = set()

    log(f"矩阵共 {len(combos)} 个组合，已完成 {len(done)} 个")
    risk_streak = 0  # 连续风控计数，成功一次就清零

    for idx, (kw, city) in enumerate(combos, 1):
        if (kw, city) in done:
            continue  # 上次已经跑过，跳过
        if len(master) >= target:
            log(f"已达到目标 {target} 条，停止")
            break
        if risk_streak >= 3:
            # 连续三轮风控说明当前 IP/账号已经被盯上，继续跑只会更糟，收手
            log("连续 3 轮风控，停止本轮（可稍后续跑）")
            break

        out_path = os.path.join(RAW, f"{kw}_{city}.csv")
        log(f"({idx}/{len(combos)}) [{city}] {kw} | 主表 {len(master)} 条")
        # 风控冷却 = 4 倍轮间隔，且至少 300 秒（轮间隔默认 25 时即 300）
        n, risk = run_one(kw, city, pages, out_path, cooldown=max(gap * 4, 300))
        if risk:
            risk_streak += 1
            continue  # 该组合不计入 done，下次重跑会再试
        risk_streak = 0

        added = 0
        if os.path.exists(out_path):
            for r in csv.DictReader(open(out_path, encoding="utf-8-sig")):
                jid = r.get("job_id")
                # 全局按 job_id 去重：别的关键词/城市已经采到过的直接跳过
                if not jid or jid in master:
                    continue
                exp, edu = split_tags(r.get("tags"))
                c, area = split_location(r.get("location"))
                # 把 scraper 的英文字段映射成主表的中文列名
                master[jid] = {
                    "job_id": jid,
                    "岗位名称": r.get("title", ""),
                    "所在城市": c or city,  # location 缺失时退回本轮的城市名
                    "区域": area,
                    "薪资范围": r.get("salary", ""),
                    "公司名称": r.get("boss_name", ""),
                    "工作经验要求": exp,
                    "学历要求": edu,
                    "岗位链接": r.get("job_link", ""),
                    "公司规模": r.get("company_scale", ""),
                    "融资阶段": r.get("company_stage", ""),
                    "所属行业": r.get("company_industry", ""),
                    "技能标签": r.get("skills", ""),
                    "福利待遇": r.get("welfare", ""),
                    "Boss活跃度": r.get("boss_active_status", ""),
                    "薪资来源": r.get("salary_source", ""),
                }
                added += 1
        done.add((kw, city))
        # 先把进度落盘再存主表：万一主表写失败，下次至少不会重复跑这个组合
        json.dump([list(x) for x in done], open(done_file, "w", encoding="utf-8"), ensure_ascii=False)
        total = save_master(master)
        log(f"  本轮 {n} 条 / 新增 {added} 条 / 主表累计 {total} 条")

        if len(master) < target:
            # 还没达标才需要休息；达标就直接进下一次循环的 break
            log(f"  轮间隔 {gap}s ...")
            time.sleep(gap)

    total = save_master(master)
    log("=" * 56)
    log(f"结束。主表 {total} 条 → {MASTER}")
    log(f"已完成组合 {len(done)}/{len(combos)}，可再次运行本脚本续跑")


if __name__ == "__main__":
    main()
