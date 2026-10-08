"""收尾：把所有分块产物（含多实例各写的 out_*_pid.json）合并进主表，并生成 Excel。

可反复运行（幂等）。
用法：python finalize_jd.py

职责
----
流水线抓完 JD 后，数据散落在两类地方：

1. 各 worker 自己写的分块 JSON：``<WORK>/out_*.json``（forward 族）与
   ``<WORK_EDGE>/out_*.json``（reverse 族）；
2. 并发写冲突时 backfill_jd.py 退写出来的「带时间戳的主表快照」：
   ``<DATASET>_含JD_<时间戳>.csv``（forward 族）与
   ``<DATASET>_含JD_edge*.csv``（edge/reverse 族）。

本脚本把这两类产物里的 JD 回填进主表 ``MASTER``（``<DATASET>_含JD.csv``），
再把主表导出成一份给人看的 Excel（``XLSX``）。

关键设计决策
------------
* **只填空，不覆盖**：主表里已经有 JD 的行一律不动，
  避免用低质量数据覆盖已经抓到的高质量数据。
* **两条 glob 都要扫**：历史踩坑——只 glob 了 edge 族、漏了 forward 族快照，
  导致少合并 313 条 JD。所以这里同时扫 ``<DATASET>_含JD_*.csv`` 和
  ``<DATASET>_含JD_edge*.csv``。
* **按文件体积从大到小处理**：体积大的快照更可能是最新、最全的，
  先处理它能让高质量数据先占位（后到的同类数据因「已有 JD」被跳过）。
* **两个 glob 存在重叠**：``<DATASET>_含JD_*.csv`` 的 ``*`` 也会匹配到
  ``_含JD_edge.csv``，重复扫描无害——回填是幂等的（已有 JD 就跳过）。
* **分块 JSON 的 JD 字段名是 ``jd``**：不是「岗位描述」也不是 ``job_description``，
  按错名字取值会得到 0 条。

副作用
------
* 覆盖写主表 ``MASTER``（UTF-8-SIG，Windows 双击 Excel 不乱码）。
* 覆盖写 Excel ``XLSX``。
* 只读工作目录，不改动任何分块产物，因此可安全反复运行。

用法示例
--------
    python finalize_jd.py
    # 换数据集：BOSS_DATASET="xxx_全量数据" python finalize_jd.py
"""

import csv
import glob
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import (DATA_DIR, DATASET, FIELDS, MASTER, MASTER_EDGE,  # noqa: E402
                    WORK, WORK_EDGE, XLSX)

OUTDIR = DATA_DIR  # 时间戳快照与主表都放在数据目录下


def main():
    """执行一次收尾：读主表 → 吸收快照与分块 JSON → 回填 JD → 写回 CSV 与 Excel。

    参数：无。所有路径取自 config，可用环境变量覆盖。

    返回值：无。

    副作用：
        * 覆盖写 ``MASTER``（主表 CSV）。
        * 覆盖写 ``XLSX``（结果 Excel）。
        * 向 stdout 打印合并数、缺失率、JD 长度等统计。

    异常：单文件读取失败会在循环内被就地捕获并跳过（仅打印提示），不影响其余文件。
    """
    # 主表是合并的「底座」，先把已有行整体读进内存。
    # utf-8-sig 用来兼容 Excel 导出的 BOM 头。
    rows = list(csv.DictReader(open(MASTER, encoding="utf-8-sig")))
    # 老版本主表可能没有「岗位描述」列，统一补成空串，
    # 后面就能无脑取值，不必再防 None / KeyError。
    for r in rows:
        r.setdefault("岗位描述", "")
    # 以 job_id 建索引，O(1) 定位目标行。
    # 注意：若主表存在重复 job_id，后面的行会覆盖前面的行。
    by_id = {r["job_id"]: r for r in rows}
    # 合并前的「有 JD 行数」，用于最后算新增量。
    before = sum(1 for r in rows if str(r.get("岗位描述", "")).strip())

    merged = 0  # 累计成功回填的 JD 条数

    # 1) 吸收所有"带时间戳的中间主表"（forward 族 + edge 族）
    #    并发写冲突时 backfill_jd.py 会退写成 `<主表>_<时间戳>.csv`。
    #    这类文件可能承载着"已落盘但主表没来得及回写"的一批 JD —— 收尾时必须一并纳入，
    #    否则会少合并几百条（历史真实踩坑）。
    edge_files = sorted(
        # reverse 主表族的快照：`<DATASET>_含JD_edge.csv` 及其带时间戳的变体
        glob.glob(os.path.join(OUTDIR, f"{DATASET}_含JD_edge*.csv")),
        # reverse=True + getsize：体积大的优先（更可能最新最全）
        key=os.path.getsize, reverse=True,
    )
    fwd_snapshots = sorted(
        # forward 主表族的快照：`<DATASET>_含JD_<时间戳>.csv`
        # （该通配的 `*` 也会匹配到 `_含JD_edge.csv`，重复扫描无害）
        glob.glob(os.path.join(OUTDIR, f"{DATASET}_含JD_*.csv")),
        key=os.path.getsize, reverse=True,
    )
    for ep in edge_files + fwd_snapshots:
        try:
            n_edge = 0  # 本文件贡献的新增条数，仅用于打印
            for r in csv.DictReader(open(ep, encoding="utf-8-sig")):
                jid = r.get("job_id")
                jd = str(r.get("岗位描述") or "").strip()
                # 三个条件都成立才回填：
                #   ① job_id 在主表里（不在 = 不属于本数据集，丢弃）；
                #   ② 快照这行确实有 JD；
                #   ③ 主表这行当前还没有 JD —— 这就是"只填空、不覆盖"的核心。
                if jid in by_id and jd and not str(by_id[jid].get("岗位描述", "")).strip():
                    by_id[jid]["岗位描述"] = jd
                    n_edge += 1
                    merged += 1
            print(f"从 {os.path.basename(ep)} 合并 {n_edge} 条 JD")
        except Exception as exc:
            # 单个快照损坏（半截文件 / 编码异常）不影响整体收尾，跳过即可
            print(f"  读取 {os.path.basename(ep)} 失败: {str(exc)[:80]}")

    # 2) 吸收两个工作目录下的分块产物
    files = []
    for d in (WORK, WORK_EDGE):  # forward 工作目录 + reverse 工作目录
        # reverse 族的产物在 WORK_EDGE；历史上漏扫这里会少合并大批 JD
        files.extend(sorted(glob.glob(os.path.join(d, "out_*.json"))))
    for f in files:
        try:
            # 每个 out_*.json 是一个数组，元素形如 {"job_id": ..., "jd": ...}
            for d in json.load(open(f, encoding="utf-8")):
                jid = d.get("job_id")
                # JD 字段名就是 `jd` —— 按「岗位描述」取会全是空
                jd = str(d.get("jd") or "").strip()
                # 与快照同样策略：只填空，不覆盖已有 JD
                if jid in by_id and jd and not str(by_id[jid].get("岗位描述", "")).strip():
                    by_id[jid]["岗位描述"] = jd
                    merged += 1
        except Exception as exc:
            print(f"  跳过 {os.path.basename(f)}: {str(exc)[:70]}")

    after = sum(1 for r in rows if str(r.get("岗位描述", "")).strip())  # 合并后的「有 JD 行数」
    print(f"分块产物文件: {len(files)} 个")
    print(f"合并前 {before} 条 → 合并后 {after} 条（新增 {merged}）")

    # 覆盖写主表。
    # extrasaction="ignore"：输入行可能带多余字段（如源表多出「办公地址」），
    # 让 DictWriter 安静忽略，而不是抛 ValueError。
    with open(MASTER, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    # 统计
    print(f"\n主表: {len(rows)} 条")
    cc = Counter(r["所在城市"] for r in rows)  # 城市分布
    print("按城市:", "  ".join(f"{k} {v}" for k, v in cc.most_common()))
    print(f"有 JD: {after} / {len(rows)} = {after * 100 // len(rows)}%")
    for k in ["岗位名称", "所在城市", "薪资范围", "公司名称", "工作经验要求", "学历要求", "岗位链接"]:
        miss = sum(1 for r in rows if not str(r[k]).strip())  # 该字段为空的条数
        print(f"  {k}: 缺失 {miss}（覆盖 {100 - miss * 100 // len(rows)}%）")
    lens = [len(str(r.get("岗位描述", "")).strip()) for r in rows]
    nz = [L for L in lens if L > 0]  # 只看有 JD 的行，避免空串拉低均值
    if nz:
        print(f"  JD 平均 {sum(nz) // len(nz)} 字，最长 {max(nz)} 字")

    # Excel
    # 局部 import：只有走到导出这步才需要这些库；即使 openpyxl 缺失，
    # 上面的 CSV 主表也早已写好，不会前功尽弃。
    import re

    import openpyxl
    from openpyxl.utils import get_column_letter

    # openpyxl 不接受控制字符，先清洗（换行/制表保留）
    # 该正则剔除除制表(\t)与换行(\n)之外的 C0 控制字符，
    # 既满足 openpyxl 要求，又保住 JD 的多行排版。
    illegal = re.compile(r"[\000-\010]|[\013-\014]|[\016-\037]")

    def clean(v):
        """清洗单个单元格值：字符串剔除非法控制字符；非字符串原样返回。"""
        return illegal.sub("", str(v)) if isinstance(v, str) else v

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "AI产品经理岗位"
    ws.append(FIELDS)  # 首行表头
    for r in rows:
        # 按 FIELDS 顺序取值并清洗（.get 兜底防缺列）
        ws.append([clean(r.get(k, "")) for k in FIELDS])
    for i, h in enumerate(FIELDS, 1):
        # 列宽按表头长度自适应，夹在 [12, 34] 之间，避免过窄或过宽
        ws.column_dimensions[get_column_letter(i)].width = min(max(len(h) + 4, 12), 34)
    ws.freeze_panes = "A2"  # 冻结首行表头，滚动时表头常驻
    wb.save(XLSX)
    print(f"\nExcel 已生成: {XLSX} ({os.path.getsize(XLSX) // 1024} KB)")


if __name__ == "__main__":
    main()
