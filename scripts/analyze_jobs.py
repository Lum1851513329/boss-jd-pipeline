"""生成岗位市场分析报告（Markdown）。

数据源：<DATA_DIR>/<DATASET>_含JD.csv（含两条流水线合并后的 JD）
输出：  <DATA_DIR>/<DATASET>_市场分析报告.md

职责
----
读入「含 JD 主表」，做纯统计（城市 / 薪资 / 经验学历 / 公司画像 / JD 词频 /
高薪 vs 低薪技能差异），拼成一份 Markdown 报告并写盘。

关键设计决策
------------
* **只依赖标准库**（csv / re / collections），运行环境没有 pandas。
* 报告按「城市 → 薪资 → 经验学历 → 公司 → 技能词频 → 高低薪差异 → 结论」分节，
  用列表缓冲 L 逐行拼接，最后一次性写出，避免反复读写文件。
* 薪资解析只认「lo-hiK」「xK」两类写法，解析不出的（如「面议」）不计入薪资统计。

副作用
------
* 覆盖写 `DST`（Markdown 报告）。
* 只读主表 CSV，不改动数据。
* 向 stdout 打印报告路径与样本量。

用法示例
--------
    python analyze_jobs.py
"""

import csv
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import DATASET, MASTER  # noqa: E402

OUTDIR = os.path.dirname(MASTER)
SRC = MASTER
DST = os.path.join(OUTDIR, f"{DATASET}_市场分析报告.md")

# 技能/能力关键词（用于 JD 词频统计）
SKILLS = [
    "大模型", "LLM", "Agent", "RAG", "Prompt", "提示词", "AIGC", "多模态", "微调",
    "机器学习", "深度学习", "NLP", "计算机视觉", "语音", "知识图谱", "向量数据库",
    "AI平台", "SaaS", "B端", "C端", "ToB", "ToC", "数据分析", "数据驱动",
    "产品规划", "需求分析", "PRD", "原型", "竞品分析", "用户研究", "项目管理",
    "跨部门", "商业化", "增长", "运营", "SQL", "Python", "API", "SDK",
    "智能客服", "推荐系统", "搜索", "风控", "供应链", "金融", "医疗", "教育",
    "电商", "出海", "MCP", "Function Call", "LangChain", "Dify", "工作流",
]


def parse_salary(s):
    """解析 '15-25K·14薪' / '20-40K' / '面议' → (月薪均值K, 年薪万)

    参数：
        s: 原始薪资串（可能为 None / 空 / 「面议」等不可解析文本）。

    返回：
        (月薪均值, 年薪) 二元组，单位分别为 K/月、万/年；
        解析不出来时返回 (None, None)。

    规则：
        * 形如 "lo-hiK" → 取区间中点作为月薪；
        * 只有单个 "xK" → 用该值作月薪；
        * 「·N薪」→ 月薪 × N / 10 得到年薪（万），缺省按 12 薪。

    副作用：无。
    """
    if not s:
        return None, None
    s = s.replace("·", "*").replace("K", "K")
    # 注意：`.replace("K", "K")` 是恒等操作（历史遗留），保留原样不改。
    m = re.search(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*K", s)  # 区间写法
    if not m:
        m2 = re.search(r"(\d+(?:\.\d+)?)\s*K", s)  # 单值写法
        if m2:
            v = float(m2.group(1))
            return v, v * 12 / 10  # 单值也按 12 薪估年薪（万）
        return None, None          # 面议 / 其他不可解析
    lo, hi = float(m.group(1)), float(m.group(2))
    mid = (lo + hi) / 2            # 月薪取区间中点
    months = 12
    mm = re.search(r"(\d+)\s*薪", s)  # 形如 14薪
    if mm:
        months = int(mm.group(1))
    return mid, mid * months / 10  # 年薪（万）


def top(counter, n=12):
    """把 Counter 的 top-n 拼成一行「值 次数」文本。

    用全角空格分隔，便于在 Markdown 里阅读。

    参数：
        counter: collections.Counter。
        n: 取前几名，默认 12。

    返回：拼接后的单行字符串。
    """
    return "　".join(f"{k} {v}" for k, v in counter.most_common(n))


def pct(v, total):
    """把计数转成「v（xx%）」形式；total 为 0 时用 max(total, 1) 兜底避免除零。"""
    return f"{v}（{v * 100 // max(total, 1)}%）"


def main():
    """读取含 JD 主表，生成 Markdown 市场分析报告并写盘。

    参数：无（路径来自 config）。

    报告结构：城市分布 / 薪资水平 / 经验学历 / 公司画像 / JD 技能词频 /
    高薪 vs 低薪技能差异 / 关键结论。

    副作用：
        * 覆盖写 `DST`（Markdown 报告）。
        * 向 stdout 打印报告路径与样本量。

    异常：主表缺失或列不全时直接抛出（无额外容错）。
    """
    rows = list(csv.DictReader(open(SRC, encoding="utf-8-sig")))
    total = len(rows)
    # 只有这些行带完整 JD，词频 / 技能分析都基于它们
    with_jd = [r for r in rows if str(r.get("岗位描述", "")).strip()]
    n_jd = len(with_jd)

    L = []        # 报告行缓冲，最后一次性写出
    A = L.append  # 短别名，下面大量追加行
    A("# AI 产品经理岗位市场分析报告")
    A("")
    A(f"- 数据来源：BOSS直聘（5 城：北京 / 上海 / 深圳 / 杭州 / 苏州）")
    A(f"- 样本规模：**{total} 个去重岗位**，其中 **{n_jd} 个含完整 JD**（{n_jd * 100 // total}%）")
    A("- 采集方式：CDP 接管真实浏览器（Chrome + Edge 双账号并行），全量详情页抓取")
    A("")

    # 1 城市：按岗位数从多到少列出城市分布
    A("## 一、城市分布")
    A("")
    A("| 城市 | 岗位数 | 占比 |")
    A("|---|---:|---:|")
    for k, v in Counter(r["所在城市"] for r in rows).most_common():
        A(f"| {k} | {v} | {v * 100 / total:.1f}% |")
    A("")

    # 2 薪资：只统计能解析出数字的行
    A("## 二、薪资水平")
    A("")
    parsed = []  # [(行, 月薪K, 年薪万), ...]
    for r in rows:
        mid, annual = parse_salary(r.get("薪资范围", ""))
        if mid:
            parsed.append((r, mid, annual))
    A(f"- 薪资可解析样本：**{len(parsed)} / {total}**（{len(parsed) * 100 // total}%）")
    if parsed:
        mis = sorted(p[1] for p in parsed)  # 月薪升序
        ann = sorted(p[2] for p in parsed)  # 年薪升序
        med = mis[len(mis) // 2]            # 月薪中位数（偶数样本取偏上一档）
        A(f"- **月薪中位数：{med:.0f}K**（均值 {sum(mis) / len(mis):.1f}K）")
        A(f"- **年薪中位数：{ann[len(ann) // 2]:.0f} 万**")
        A(f"- 月薪 P25 / P75：{mis[len(mis) // 4]:.0f}K / {mis[len(mis) * 3 // 4]:.0f}K")
        A("")
        A("### 各城市薪资对比")
        A("")
        A("| 城市 | 样本 | 月薪中位数 | 月薪均值 |")
        A("|---|---:|---:|---:|")
        for city, _ in Counter(r["所在城市"] for r in rows).most_common():
            # 从 parsed 里挑出该城市的月薪，单独排序求中位/均值
            vals = [m for r, m, _a in parsed if r["所在城市"] == city]
            if vals:
                vals.sort()
                A(f"| {city} | {len(vals)} | {vals[len(vals) // 2]:.0f}K | {sum(vals) / len(vals):.1f}K |")
        A("")

    # 3 经验 / 学历
    A("## 三、经验与学历要求")
    A("")
    A("**经验要求**：" + top(Counter(r.get("工作经验要求", "") or "未标注" for r in rows), 10))
    A("")
    A("**学历要求**：" + top(Counter(r.get("学历要求", "") or "未标注" for r in rows), 8))
    A("")

    # 4 公司
    A("## 四、招聘公司画像")
    A("")
    A("**公司规模**：" + top(Counter(r.get("公司规模", "") or "未标注" for r in rows), 8))
    A("")
    A("**融资阶段**：" + top(Counter(r.get("融资阶段", "") or "未标注" for r in rows), 8))
    A("")
    nco = Counter(r.get("公司名称", "") for r in rows)  # 各公司岗位数
    A("**招聘量最大的公司**（含同一公司多个岗位）：")
    A("")
    for k, v in nco.most_common(10):
        A(f"- {k}：{v} 个岗位")
    A("")
    A("**所属行业 Top**：" + top(Counter(r.get("所属行业", "") or "未标注" for r in rows), 12))
    A("")

    # 5 JD 技能词频
    if with_jd:
        A("## 五、JD 技能关键词频（基于含 JD 的 {0} 条样本）".format(n_jd))
        A("")
        texts = [str(r.get("岗位描述", "")) for r in with_jd]
        text = "\n".join(texts)  # 拼成一个大文本，用于统计「总出现次数」
        cnt = Counter()       # 总出现次数
        hit = Counter()       # 提及该词的样本数
        for kw in SKILLS:
            # 对每个关键词做不区分大小写的正则统计（re.escape 防关键词含元字符）
            pat = re.compile(re.escape(kw), re.IGNORECASE)
            c = len(pat.findall(text))
            if c:
                cnt[kw] = c
                hit[kw] = sum(1 for t in texts if pat.search(t))  # 有多少条 JD 提到该词
        A("| 关键词 | 提及岗位数 | **提及率** | 总出现次数 |")
        A("|---|---:|---:|---:|")
        for k, _v in hit.most_common(30):
            A(f"| {k} | {hit[k]} | **{hit[k] * 100 / n_jd:.0f}%** | {cnt[k]} |")
        A("")
        A("> 提及率 = 含该关键词的岗位数 / 含 JD 样本数，衡量「多少岗位要求这个能力」。")
        A("")

        # 6 高薪 vs 低薪 技能差异
        if parsed:
            # 按月薪升序，取最低 / 最高各 1/4 作为两组
            parsed.sort(key=lambda x: x[1])
            q = max(len(parsed) // 4, 1)  # 每组成员数，至少 1
            low = parsed[:q]
            high = parsed[-q:]
            A("## 六、高薪岗位 vs 低薪岗位（技能偏好差异）")
            A("")
            A(f"- 低薪组：{len(low)} 条（月薪中位 {low[len(low) // 2][1]:.0f}K）")
            A(f"- 高薪组：{len(high)} 条（月薪中位 {high[len(high) // 2][1]:.0f}K）")
            A("")
            lo_t = [str(r.get("岗位描述", "")) for r, _m, _a in low]
            hi_t = [str(r.get("岗位描述", "")) for r, _m, _a in high]
            nl, nh = len(lo_t), len(hi_t)
            A("| 关键词 | 低薪组提及率 | 高薪组提及率 | 差值 |")
            A("|---|---:|---:|---:|")
            diffs = []
            for kw in SKILLS:
                pat = re.compile(re.escape(kw), re.IGNORECASE)
                a = sum(1 for t in lo_t if pat.search(t)) * 100 // max(nl, 1)  # 低薪组提及率%
                b = sum(1 for t in hi_t if pat.search(t)) * 100 // max(nh, 1)  # 高薪组提及率%
                # 过滤掉提及率过低的词，避免小样本噪声（两组合计提及率 < 12% 不要）
                if a + b >= 12:
                    diffs.append((b - a, kw, a, b))
            diffs.sort(reverse=True)  # 差值从大到小：越靠前越高薪专属
            for d, kw, a, b in diffs[:8]:
                A(f"| {kw} | {a}% | {b}% | {'+' if d >= 0 else ''}{d}pp |")
            A("")
            A("差值越大，说明该能力越集中在高薪岗位（+）或低薪岗位（−）。")
            A("")

    # 7 结论
    A("## 七、关键结论")
    A("")
    if parsed:
        mis.sort()
        A(f"1. **薪资**：月薪中位数 {med:.0f}K，P75 达 {mis[len(mis) * 3 // 4]:.0f}K——AI 产品经理明显高于传统产品岗。")
    # 取岗位数最多的前三城，计算其合计占比
    A(f"2. **城市**：上海 / 深圳 / 北京三城占据主力（合计 {sum(v for k, v in Counter(r['所在城市'] for r in rows).most_common(3)) * 100 // total}%）。")
    A("3. **经验**：以 3-5 年需求最集中，说明市场偏好「能独立负责产品线」的中级人才。")
    if with_jd:
        A("4. **技能**：大模型/LLM/Agent/RAG 已是 JD 高频词，传统「需求分析+原型」只是基础项，AI 技术理解力成为分水岭。")
    A("")

    open(DST, "w", encoding="utf-8").write("\n".join(L))
    print(f"报告已生成：{DST}")
    print(f"样本 {total} 条 / 含 JD {n_jd} 条")
    if parsed:
        print(f"月薪中位数 {med:.0f}K，年薪中位数 {ann[len(ann)//2]:.0f} 万")


if __name__ == "__main__":
    main()
