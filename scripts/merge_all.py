"""把 matrix_raw 下的所有原始 CSV 合并成一张主表（按 job_id 去重）。

可反复运行：每次采集后调用即可刷新主表，不会重复计数。
用法：python merge_all.py

职责
----
矩阵采集会按「关键词 × 城市」在 ``_方案2测试/matrix_raw`` 下生成一堆原始 CSV，
本脚本把它们全部读进来，按 job_id 去重后写成一张「列表主表」（``SRC``）。

注意：这里的主表是**列表主表**（不含 JD），不是 finalize_jd.py 那个含 JD 的主表——
本脚本只做「列表级」的朴素合并，JD 是后面 backfill_jd.py + finalize_jd.py 才补的。

关键设计决策
------------
* **按 job_id 去重，先到先得**：同一 job_id 多次出现时保留最先扫到的那个，
  既不覆盖也不跨文件合并字段。
* **文件名推断城市优先**：matrix_raw 的命名规则是「关键词_城市.csv」，
  文件名里的城市比行内 location 更权威（location 有时只有区县）。
* **原子写**：先写 ``<MASTER>.tmp`` 再 ``os.replace``，避免中途失败把主表写坏。
* **两条扫描目录**：先扫 ``matrix_raw`` 子目录，再扫其父目录兜底；目录不存在时
  glob 自然返回空，不报错。

副作用
------
* 覆盖写 `SRC`（列表主表 CSV，UTF-8-SIG）。
* 只读原始 CSV，不改动来源文件。

用法示例
--------
    python merge_all.py
    # 换数据目录：BOSS_DATA_DIR=... python merge_all.py
"""

import csv
import glob
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import DATA_DIR, FIELDS_LIST, SRC  # noqa: E402

OUTDIR = DATA_DIR
# 扫描顺序即「先到先得」的优先级：matrix_raw 子目录在前，父目录兜底。
# 目录名里的「方案2测试」是历史命名，保留不改。
RAW_DIRS = [
    os.path.join(OUTDIR, "_方案2测试", "matrix_raw"),
    os.path.join(OUTDIR, "_方案2测试"),
]
MASTER = SRC  # 注意：这里的"主表"是列表主表（SRC），不含 JD
CITIES = ["北京", "上海", "深圳", "杭州", "苏州"]

FIELDS = FIELDS_LIST  # 列表字段（不含「岗位描述」），故本脚本产出天然无 JD


def split_tags(tags):
    """把原始 tags 字段（形如「3-5年|本科|...」的「|」分隔串）拆成经验、学历两项。

    参数：
        tags: 原始标签串，可能为 None / 空。

    返回：
        (exp, edu) 二元组，均为字符串（可能为空串）。

    规则：
        * 含「年 / 应届 / 经验」的段落 → 经验；
        * 含「本科 / 大专 / 硕士 / ...」的段落 → 学历；
        * 两类都没命中时兜底：经验取第 1 段、学历取第 2 段（源站标签顺序的惯例）。

    副作用：无。对 None / 空串做了容错，不抛异常。
    """
    parts = [p.strip() for p in str(tags or "").split("|") if p.strip()]
    exp = edu = ""
    for p in parts:
        if re.search(r"年|应届|经验", p):
            exp = exp or p  # 只认第一个命中的，避免被后面的噪声覆盖
        elif re.search(r"本科|大专|硕士|博士|中专|高中|初中|学历", p):
            edu = edu or p
    if not exp and parts:           # 兜底：经验默认取第 1 段
        exp = parts[0]
    if not edu and len(parts) > 1:  # 兜底：学历默认取第 2 段
        edu = parts[1]
    return exp, edu


def split_location(loc):
    """把 location 字段（形如「上海·浦东新区·张江」）拆成 (城市, 区域)。

    参数：
        loc: 原始位置串，可能为 None / 空。

    返回：
        (城市, 区域) 二元组；城市是第 1 段，区域是其余段的「·」拼接。
        输入为空时返回 ("", "")。

    副作用：无。
    """
    parts = [p for p in str(loc or "").split("·") if p]
    if not parts:
        return "", ""
    return parts[0], "·".join(parts[1:])


def guess_city(path, row):
    """从文件名推断城市（matrix_raw 命名规则：关键词_城市.csv）

    文件名的优先级高于行内 location：矩阵采集时文件名里的城市是权威来源，
    而 location 有时为空或只到区县。

    参数：
        path: 该 CSV 的路径。
        row:  当前行（DictReader 出来的一行）。

    返回：
        城市名（命中 CITIES 之一则为文件名推导结果，否则退回 location 第 1 段）。
    """
    name = os.path.splitext(os.path.basename(path))[0]
    for c in CITIES:
        if name.endswith("_" + c) or name == c:
            return c
    c, _ = split_location(row.get("location"))  # 文件名没命中 → 退回位置字段
    return c


def main():
    """扫描 RAW_DIRS 下所有原始 CSV，按 job_id 去重合并成列表主表并落盘。

    参数：无（目录 / 输出路径来自 config）。

    去重策略：**先到先得，只取第一条**（`if jid in master: continue`）。
    同一个 job_id 在多个文件出现时，保留最先扫到的那个；不覆盖、不合并字段。
    由于文件序为 sorted()，最终结果对「文件名排序」敏感。

    副作用：
        * 原子写主表：先写 `<MASTER>.tmp`，成功后再 os.replace 覆盖 `MASTER`，
          中途崩溃不会留下半截主表。
        * 向 stdout 打印每个文件的增量与整体统计。
    """
    master = {}  # job_id -> 该岗位的行字典（去重后的唯一集合）
    files = []
    for d in RAW_DIRS:
        # sorted() 决定「先到先得」里谁先谁后
        files.extend(sorted(glob.glob(os.path.join(d, "*.csv"))))

    skipped = 0  # 被跳过的文件数（缺 job_id 列 / 读取异常）
    for path in files:
        try:
            reader = csv.DictReader(open(path, encoding="utf-8-sig"))
            # 没有 job_id 列的表（导出的汇总表、说明表等）直接跳过
            if not reader.fieldnames or "job_id" not in reader.fieldnames:
                skipped += 1
                continue
            n_add = 0  # 本文件新增（去重后）的条数
            for r in reader:
                jid = (r.get("job_id") or "").strip()
                # 空 job_id 或已出现过 → 跳过（这就是按 job_id 去重的实现）
                if not jid or jid in master:
                    continue
                exp, edu = split_tags(r.get("tags"))          # 原始标签 → 经验 / 学历
                city = guess_city(path, r)                    # 文件名优先，location 兜底
                _, area = split_location(r.get("location"))   # 只取区域（城市已单独推断）
                master[jid] = {
                    "job_id": jid,
                    "岗位名称": r.get("title", ""),
                    "所在城市": city,
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
                n_add += 1
            print(f"  {os.path.relpath(path, OUTDIR):58s} +{n_add}")
        except Exception as exc:
            print(f"  跳过 {path}: {exc}")
            skipped += 1

    # 原子写：先落临时文件再替换，避免中途失败把主表写坏。
    # tmp 文件名**带 pid**：本脚本可能被并发触发，固定名 `.tmp` 会互相覆盖。
    # 替换重试 5 次：Windows 上主表被 Excel / 预览面板占用是常态，
    # 不重试就会直接抛 PermissionError 中断整轮。
    tmp = f"{MASTER}.{os.getpid()}.tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in master.values():
            w.writerow(r)
    for attempt in range(5):
        try:
            os.replace(tmp, MASTER)
            break
        except PermissionError:
            print(f"  主表被占用，{1 + attempt}s 后重试（{attempt + 1}/5）")
            time.sleep(1 + attempt)
    else:
        print(f"  ⚠️ 主表写入失败，结果保留在 {tmp}")

    from collections import Counter  # 局部 import：只有统计阶段才用到
    print("=" * 70)
    print(f"原始文件 {len(files)} 个（跳过 {skipped}）")
    print(f"主表 {len(master)} 条 → {MASTER}")
    if master:
        cc = Counter(r["所在城市"] for r in master.values())  # 城市分布
        print("按城市:", "  ".join(f"{k} {v}" for k, v in cc.most_common()))
        # 关键字段的缺失条数；此处 f 是字典推导的临时变量，与上面写文件的文件句柄无关
        miss = {f: sum(1 for r in master.values() if not str(r[f]).strip()) for f in
                ["岗位名称", "所在城市", "薪资范围", "公司名称", "工作经验要求", "学历要求", "岗位链接"]}
        print("缺失统计:", miss)


if __name__ == "__main__":
    main()
