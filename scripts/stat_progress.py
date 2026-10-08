# -*- coding: utf-8 -*-
"""JD 补抓进度统计（只读，绝不修改任何数据）。

为什么不能用「主表非空数」当进度
--------------------------------
这套流水线在并发写冲突时会退写成 **带时间戳的中间产物**（`..._含JD_<ts>.csv`），
且分块产出的 `out_*.json` 要到**块结束**才合并进主表。所以：

    真实进度 = 全部 CSV 变体并集 ∪ 两泳道在途 out_*.json 里"不在并集内"的 job_id

只看两个 canonical CSV 会严重低估（历史实测低估过 545 条）。

判定口径
--------
* `forward`  = forward 族 CSV ∪ forward 在途
* `reverse`  = (edge 族 CSV ∪ edge 在途) − forward
  （edge 表是从主表复制起步的，直接相加会重复计数，必须取"独有增量"）
* `DONE`    = forward ∪ reverse，且全部落在源表 job_id 全集内（越界应报 0）

用法：
    python stat_progress.py             # 看进度
    python stat_progress.py --verbose   # 额外打印每个文件的贡献
"""

import csv
import glob
import io
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import (BLOCK_KEYWORDS, DATA_DIR, DATASET, MASTER, MASTER_EDGE,  # noqa: E402
                    SRC, WORK, WORK_EDGE)


def read_ids(path, with_jd_only=True):
    """读 CSV，返回 {job_id: 是否有 JD}。"""
    out = {}
    with io.open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            jid = (row.get("job_id") or "").strip()
            if not jid:
                continue
            if with_jd_only:
                out[jid] = bool((row.get("岗位描述") or "").strip())
            else:
                out[jid] = True
    return out


def jd_ids(path):
    return {k for k, v in read_ids(path).items() if v}


def inflight_ids(work_dir, known):
    """在途 out_*.json 中"不在 known 集合内"的有效 job_id。"""
    new = set()
    if not os.path.isdir(work_dir):
        return new, []
    detail = []
    for p in sorted(glob.glob(os.path.join(work_dir, "out_*.json"))):
        if os.path.basename(p).startswith(("out_verify", "out_diag")):
            continue
        try:
            data = json.load(io.open(p, encoding="utf-8"))
        except Exception as exc:
            detail.append((os.path.basename(p), f"读取失败 {str(exc)[:40]}"))
            continue
        items = data.get("results") if isinstance(data, dict) else data
        if not isinstance(items, list):
            continue
        got = set()
        for it in items:
            if not isinstance(it, dict):
                continue
            jid = str(it.get("job_id") or it.get("id") or "").strip()
            jd = str(it.get("jd") or it.get("岗位描述") or "").strip()
            if jid and jd and jid not in known:
                got.add(jid)
        if got:
            detail.append((os.path.basename(p), f"new={len(got)}"))
            new |= got
    return new, detail


def scan_blocked(logfile):
    """扫日志里的风控关键词；返回命中的行。"""
    if not os.path.exists(logfile):
        return None
    try:
        text = io.open(logfile, encoding="utf-8", errors="replace").read()
    except Exception:
        return None
    pat = re.compile("|".join(re.escape(k) for k in BLOCK_KEYWORDS))
    return [ln for ln in text.splitlines() if pat.search(ln)]


def main():
    verbose = "--verbose" in sys.argv

    # 1) 全集
    if not os.path.exists(SRC):
        print(f"源表不存在: {SRC}")
        sys.exit(1)
    universe = {k for k, _ in read_ids(SRC, with_jd_only=False).items()}

    # 2) 所有 CSV 变体，按 forward / edge 分族
    fwd_csv, edge_csv = set(), set()
    fwd_files, edge_files = [], []
    for p in sorted(glob.glob(os.path.join(DATA_DIR, f"{DATASET}_含JD*.csv"))):
        ids = jd_ids(p)
        (edge_files if "_edge" in os.path.basename(p) else fwd_files).append(
            (os.path.basename(p), len(ids)))
        (edge_csv if "_edge" in os.path.basename(p) else fwd_csv).__ior__(ids)
    csv_union = fwd_csv | edge_csv

    # 3) 在途
    fwd_in, fwd_detail = inflight_ids(WORK, csv_union)
    edge_in, edge_detail = inflight_ids(WORK_EDGE, csv_union | fwd_in)

    forward = fwd_csv | fwd_in
    reverse = (edge_csv | edge_in) - forward
    done = forward | reverse
    missing = universe - done
    outside = done - universe

    total = len(universe) or 2445
    print(f"DONE={len(done)}/{total} ({len(done) * 100.0 / total:.1f}%)  "
          f"missing={len(missing)}")
    print(f"  forward            = {len(forward)}")
    print(f"  reverse(独有增量)   = {len(reverse)}")
    print(f"  CSV 并集           = {len(csv_union)}  "
          f"(forward族 {len(fwd_csv)} / edge族 {len(edge_csv)})")
    print(f"  在途新增           = {len(fwd_in) + len(edge_in)}  "
          f"(forward {len(fwd_in)} / edge {len(edge_in)})")
    print(f"  越界 job_id        = {len(outside)}  （应为 0，否则并集在虚增）")

    if verbose:
        print("\n-- forward 族 CSV --")
        for n, c in fwd_files:
            print(f"   {n:60s} {c}")
        print("-- edge 族 CSV --")
        for n, c in edge_files:
            print(f"   {n:60s} {c}")
        print("-- 在途文件 --")
        for n, c in fwd_detail + edge_detail:
            print(f"   {n:42s} {c}")
        if missing:
            print(f"\n-- 缺失样例（最多 12 个）--\n   {sorted(missing)[:12]}")

    # 4) 风控扫描
    print("\n-- 风控扫描 --")
    for label, lf in (("forward", os.path.join(WORK, "backfill.log")),
                      ("reverse", os.path.join(WORK_EDGE, "backfill_edge.log"))):
        hits = scan_blocked(lf)
        if hits is None:
            print(f"  {label}: 日志不存在（该泳道未由本套脚本启动？）")
        elif hits:
            print(f"  {label}: ⚠️ 命中 {len(hits)} 行受限关键词")
            for ln in hits[-3:]:
                print(f"      {ln[:140]}")
        else:
            print(f"  {label}: ✅ 无受限关键词")


if __name__ == "__main__":
    main()
