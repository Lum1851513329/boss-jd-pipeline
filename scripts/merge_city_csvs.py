"""合并 _新6城 下各条流水线的 CSV，输出最终 6 城数据集（按 job_id 去重）。

用法：python merge_city_csvs.py [输出CSV]

职责
----
`_新6城` 工作目录下每条城市流水线各写一个 CSV，本脚本把它们合并成一张
6 城总表（`<KEYWORD>_新6城_含JD.csv`）。

去重与取舍策略（与 finalize_jd.py / merge_all.py 刻意不同）
----------------------------------------------------------
本脚本采用「**新数据覆盖旧数据，但有 JD 的优先**」：只有当「旧行缺失」或
「旧行没 JD 而新行有 JD」时才替换。同一 job_id 反复出现时，带 JD 的版本会胜出；
都带 JD（或都不带）时后到者胜。这与 finalize_jd.py / merge_all.py 的
「只填空、不覆盖已有」正好相反——那边怕低质量覆盖高质量，这边要的就是
「较新的行优先、同时保住 JD」。

副作用
------
* 覆盖写输出 CSV（UTF-8-SIG）。
* 只读 `_新6城` 下的流水线 CSV，不改动来源文件。

用法示例
--------
    python merge_city_csvs.py
    python merge_city_csvs.py out.csv
"""

import csv
import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import DATA_DIR, FIELDS, KEYWORD  # noqa: E402

WORK = os.environ.get("BOSS_CITY_WORK") or os.path.join(DATA_DIR, "_新6城")  # 6 城流水线工作目录
DEFAULT_OUT = os.path.join(DATA_DIR, f"{KEYWORD}_新6城_含JD.csv")            # 缺省输出路径

# 城市名直接取自 BOSS 直聘站点的城市列表（数据列取值需与来源一致才能匹配）
# 「香港」即中国香港，此处沿用站点的短名。
CITIES = ["广州", "成都", "武汉", "长沙", "南京", "香港"]


def main():
    """合并 `_新6城` 下所有非 merged 开头的 CSV，去重后写 6 城总表。

    参数（命令行）：
        argv[1]（可选）：输出路径；缺省用 DEFAULT_OUT。
        所有 `--` 开头的参数都会被过滤掉（当前未实现任何开关）。

    返回值：无。

    去重策略：新数据覆盖旧数据，但带 JD 的行优先
        （`not old or (旧行无 JD and 新行有 JD)` 才替换）。

    副作用：
        * 覆盖写输出 CSV（UTF-8-SIG）。
        * 向 stdout 打印来源文件、去重后总数、各城市分布。

    异常：单个来源文件读取失败会被捕获跳过，不影响其余文件。
    """
    out = DEFAULT_OUT
    # TODO: 模块 docstring 提到的 `--append-to` 并未实现——这里只过滤掉 `--` 开头的参数，
    #       其后的路径会被当成位置参数（即输出路径），语义与文档不符，需留意。
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if args:
        out = args[0]

    merged = {}  # job_id -> 行字典（去重后的唯一集合）
    srcs = []    # [(文件名, 原始行数), ...]，仅用于最后打印来源清单
    for f in sorted(glob.glob(os.path.join(WORK, "*.csv"))):
        # 跳过本脚本上一轮产出的 merged* 文件，避免把输出又读回来当输入
        if os.path.basename(f).startswith("merged"):
            continue
        try:
            rows = list(csv.DictReader(open(f, encoding="utf-8-sig")))
        except Exception as exc:
            print(f"  跳过 {os.path.basename(f)}: {str(exc)[:60]}")
            continue
        srcs.append((os.path.basename(f), len(rows)))
        for r in rows:
            jid = r.get("job_id")
            if not jid:  # 无 job_id 无法参与去重，直接丢
                continue
            old = merged.get(jid)
            # 有 JD 的优先
            # 替换条件：① 该 job_id 首次出现；② 旧行没 JD 而新行有 JD。
            # 反过来说，旧行已有 JD 时绝不被「没 JD 的新行」覆盖。
            if not old or (not str(old.get("岗位描述", "")).strip() and str(r.get("岗位描述", "")).strip()):
                merged[jid] = r

    rows = list(merged.values())
    # extrasaction="ignore"：来源行可能带多余列，写时不报错
    with open(out, "w", newline="", encoding="utf-8-sig") as fo:
        w = csv.DictWriter(fo, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    print("来源文件：")
    for n, c in srcs:
        print(f"  {n}: {c} 条")
    print(f"\n合并去重后：{len(rows)} 条 → {out}")
    print(f"带 JD：{sum(1 for r in rows if str(r.get('岗位描述','')).strip())} 条")

    print("\n各城市分布：")
    for c in CITIES:
        sub = [r for r in rows if r.get("所在城市") == c]
        if sub:
            wjd = sum(1 for r in sub if str(r.get("岗位描述", "")).strip())
            print(f"  {c}: {len(sub)} 条（带 JD {wjd}）")


if __name__ == "__main__":
    main()
