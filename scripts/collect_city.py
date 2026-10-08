"""按「分批 + 冷却」策略采集单个城市的 BOSS 直聘岗位。

流水线位置
==========
属于**采集列表 + JD**阶段，但走的是 `boss-agent` CLI 的分批路线：
先把搜索结果（拿 job_id）和 `crawl` 采集（拿列表 + JD）拆成两件事，
每轮 burst 页搜索完就冷却，再跑一轮采集。适合单城市、需要慢慢磨的场景。

每城 target_pages 页，分若干 burst 轮次，轮间冷却 cooldown 秒；
风控命中（code 37）时自动冷却并重试；浏览器掉线时自动重新拉起。

关键设计决策
------------
  * **必须复用带界面的已登录浏览器**（CDP 方式接管）：BOSS 会对无头 /
    独立 profile 的浏览器做风控，脚本自己起的新浏览器基本拿不到数据，
    所以 ensure_browser 拉起的是 PROFILE_CHROME 这个已登录的 profile。
  * **search 与 crawl 必须分开跑**：混在一起时采集页会拿到空结果。
  * **resume 遵守创建时的 --pages 上限**：所以第一轮 crawl run 直接按
    本轮需要的页数开出 run，之后被打断就用 resume 接着跑同一个 run。
  * **crawl 会把浏览器关掉**：所以每轮采集前都得先 ensure_browser 确保
    CDP 端口上还有浏览器。

重要副作用
----------
  * 通过 subprocess 调用外部 CLI `boss.exe`（pip 包 boss_agent_cli），
    并可能在 CDP 端口上拉起一个带界面的浏览器进程。
  * 中途结果落在 `<AGENT_DIR>/crawl/runs/<run_id>/jobs.csv`（由 CLI 写）。
  * 最终结果写到 `<DATA_DIR>/boss_<关键词>_<城市>.csv`。

用法：python collect_city.py <城市> [总页数] [每轮页数] [冷却秒数]
  * 城市      必填
  * 总页数    默认 10
  * 每轮页数  默认 5（一轮里连续搜 5 页，再冷却）
  * 冷却秒数  默认 480
"""

import csv
import json
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import (AGENT_DIR, CHROME_DEFAULT, DATA_DIR, KEYWORD,  # noqa: E402
                    PROFILE_CHROME)

# 依赖 boss-agent CLI（pip 包 boss_agent_cli）。它在自己的 Scripts 目录里，
# 由 BOSS_CLI 覆盖；找不到时就退回同目录的 python -m 方式。
BOSS = os.environ.get("BOSS_CLI") or os.path.join(
    os.path.dirname(sys.executable), "boss.exe")
CHROME = os.environ.get("BOSS_BROWSER_PATH") or CHROME_DEFAULT
DD = AGENT_DIR           # CLI 的数据目录（crawl run 的产物也落在这下面）
PROFILE = PROFILE_CHROME  # 复用已登录的 profile，别用干净 profile
OUTDIR = DATA_DIR
CDP_PORT = int(os.environ.get("BOSS_CDP_PORT", "9222"))
CDP = f"http://127.0.0.1:{CDP_PORT}"

QUERY = KEYWORD
# BOSS 的岗位详情页有固定 URL 形态，用 job_id 直接拼；搜索接口只给 id 不给链接
JOB_URL = "https://www.zhipin.com/job_detail/{}.html"
# 让 CLI 接管已有的浏览器而不是自己起一个（风控关键：见模块 docstring）
CDP_ARGS = ["--cdp-url", CDP, "--browser-source", "existing-browser"]

FIELDS = [
    "岗位名称", "所在城市", "薪资范围", "公司名称",
    "岗位描述", "工作经验要求", "学历要求", "岗位链接",
    "公司规模", "所属行业", "办公地址", "技能标签", "福利待遇",
]


def log(msg):
    """带时间戳打印一行进度日志。

    参数 msg: 要打印的文本。flush=True 保证重定向到文件时也实时可见。
    """
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def boss(args, timeout=1800):
    """调用外部 CLI `boss.exe` 并解析它的 JSON 结果。

    参数:
      args    : 追加在固定前缀之后的子命令与参数
      timeout : 子进程超时秒数（crawl 这类长任务要传大一点）

    返回: 解析出的 dict（约定含 ok / error / data 三个键）。
    解析失败时返回 {"ok": False, "error": {"code": "NO_JSON", ...}}，
    而不是抛异常，让上层统一走失败分支。

    坑：`--json` 模式下 CLI 仍会打印进度条等杂输出，所以不能直接 json.loads
    整个 stdout，而是**从后往前找第一行以 `{` 开头的**，那才是结果 JSON。
    """
    env = dict(os.environ, PYTHONUTF8="1")
    p = subprocess.run([BOSS, "--json", "--data-dir", DD] + CDP_ARGS + args,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env, timeout=timeout)
    for line in reversed((p.stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except Exception:
                pass
    return {"ok": False, "error": {"code": "NO_JSON",
            "message": ((p.stdout or "") + (p.stderr or ""))[-300:]}, "data": None}


def cdp_alive():
    """探测 CDP 端口上是否有可用的浏览器（HTTP 200 即认为在线）。

    返回: True / False。任何网络异常都当作「不在线」。
    """
    try:
        with urllib.request.urlopen(CDP + "/json/version", timeout=4) as r:
            return r.status == 200
    except Exception:
        return False


def ensure_browser():
    """确保 CDP 端口上有已登录的带界面浏览器（crawl 结束会把它关掉）。

    返回: True 表示就绪；False 表示尝试拉起但没起来（调用方会失败重试）。

    副作用: 可能启动一个新的浏览器进程（带远程调试端口），并阻塞等待最多
    约 40 秒 + 3 秒；浏览器用 PROFILE 这个已登录 profile，不是干净实例。

    说明: 这里的 probe 页固定用北京城市码 101010100，只是为了把浏览器开起来
    并落到搜索页；真正要采哪个城市由后续 search 的 --city 决定，不受影响。
    """
    if cdp_alive():
        return True
    log("浏览器不在线，重新拉起...")
    import urllib.parse
    probe = ("https://www.zhipin.com/web/geek/jobs?query="
             + urllib.parse.quote(QUERY) + "&city=101010100")
    # --disable-http2 是为了绕开 BOSS 在 HTTP/2 下的一些连接异常
    # creationflags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP：
    # 让浏览器脱离本进程独立存活，父进程退出不会连带把它杀掉
    subprocess.Popen([CHROME, f"--remote-debugging-port={CDP_PORT}", "--disable-http2",
                      f"--user-data-dir={PROFILE}", "--no-first-run",
                      "--no-default-browser-check", "--window-size=1200,900", probe],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=0x00000008 | 0x00000200)
    # 最多等 20 * 2 = 40 秒，每 2 秒探一次 CDP 是否就绪
    for _ in range(20):
        time.sleep(2)
        if cdp_alive():
            log("浏览器已就绪")
            time.sleep(3)  # 端口通了不代表页面加载完，再给它 3 秒
            return True
    log("浏览器拉起失败")
    return False


def is_risk(res):
    """判断 CLI 返回的结果是否属于风控/被拦。

    参数 res: boss() 返回的 dict。
    返回: True 表示疑似风控。

    命中条件: 错误码含 "37"（code 37 是 BOSS 的典型风控码），
    或错误信息里含 风控 / 安全验证 / 异常。
    """
    code = str(((res.get("error") or {}).get("code") or ""))
    msg = str(((res.get("error") or {}).get("message") or ""))
    return "37" in code or "风控" in msg or "安全验证" in msg or "异常" in msg


def search_page(city, pg, idmap):
    """取一页搜索结果，写入映射；返回 (成功?, 条数)。

    参数:
      city  : 城市名
      pg    : 页码（从 1 开始）
      idmap : 输出参数，键为 (岗位名, 公司名)、值为 job_id 的字典

    返回: (True, 本页条数) 或 (False, 0)。

    副作用: 修改 idmap；可能拉起浏览器、阻塞 sleep（风控 300s / 普通失败 20s）。

    说明: search 接口不返回岗位链接，只返回 job_id，所以这里先把
    (岗位名, 公司名) -> job_id 记下来，最后合并 crawl 结果时再拼出链接。
    """
    for attempt in range(3):
        ensure_browser()
        res = boss(["search", QUERY, "--city", city, "--page", str(pg), "--no-cache"], timeout=300)
        if res.get("ok"):
            jobs = res.get("data") or []
            for j in jobs:
                key = (str(j.get("title", "")).strip(), str(j.get("company", "")).strip())
                idmap[key] = j.get("job_id") or ""
            return True, len(jobs)
        if is_risk(res):
            # 风控冷却固定 300s，比普通失败等得久得多
            log(f"  p{pg} 命中风控，冷却 300s 后重试（第 {attempt + 1} 次）")
            time.sleep(300)
            continue
        log(f"  p{pg} 失败: {((res.get('error') or {}).get('message') or '')[:110]}")
        time.sleep(20)
    return False, 0


def main():
    """入口：把总页数切成若干 burst 轮，轮流「搜索 + 采集」，最后合并输出。

    位置参数: argv[1]=城市（必填）, argv[2]=总页数(10),
    argv[3]=每轮页数(5), argv[4]=冷却秒数(480)。

    副作用: 拉起浏览器、写最终 CSV、通过 CLI 在
    `<DD>/crawl/runs/<run_id>/jobs.csv` 落盘中间结果。
    """
    city = sys.argv[1]
    target = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    burst = int(sys.argv[3]) if len(sys.argv) > 3 else 5
    cooldown = int(sys.argv[4]) if len(sys.argv) > 4 else 480
    os.makedirs(OUTDIR, exist_ok=True)

    # idmap: (岗位,公司) -> job_id；run_id: 当前 crawl run，用于后续 resume；
    # runs_seen: 本次涉及的所有 run_id（resume 会新增 run 时也都记上，最后一起汇总）
    idmap, run_id, runs_seen = {}, None, []
    # 把 1..target 页切成 [burst, burst] 的轮次区间，如 target=10/burst=5 -> [(1,5),(6,10)]
    rounds = [(s, min(s + burst - 1, target)) for s in range(1, target + 1, burst)]

    for idx, (start, end) in enumerate(rounds):
        log(f"=== [{city}] 第 {idx + 1}/{len(rounds)} 轮：搜索 p{start}-p{end} ===")
        for pg in range(start, end + 1):
            ok, n = search_page(city, pg, idmap)
            log(f"  search p{pg}: {'+' + str(n) if ok else '失败'} / 累计 {len(idmap)}")
            if ok:
                time.sleep(2.5)  # 翻页之间稍作停顿，降低触发风控的概率

        log(f"=== [{city}] 第 {idx + 1}/{len(rounds)} 轮：采集 JD 详情 ===")
        ensure_browser()
        if run_id is None:
            # 第一轮：新建 run，页数直接开成本轮区间长度（resume 不能超过这个上限）
            res = boss(["crawl", "run", QUERY, "--city", city, "--pages", str(end - start + 1), "--with-detail"], timeout=2400)
        else:
            # 后续轮：resume 同一个 run，不会新建、沿用创建时的 --pages 上限
            res = boss(["crawl", "resume", run_id], timeout=2400)
        if res.get("ok"):
            d = res["data"]
            run_id = d.get("run_id") or run_id
            if run_id and run_id not in runs_seen:
                runs_seen.append(run_id)
            log(f"  采集: {d.get('jobs_seen')} 条 / 详情 {d.get('detail_checks')} / {d.get('elapsed_seconds')}s")
        else:
            log(f"  采集失败: {((res.get('error') or {}).get('message') or '')[:140]}")

        if idx < len(rounds) - 1:
            # 最后一轮不用冷却（后面没有采集任务了）
            log(f"冷却 {cooldown}s ...")
            time.sleep(cooldown)

    log(f"=== [{city}] 合并输出 ===")
    if not runs_seen:
        log("没有任何采集结果")
        sys.exit(1)
    rows = []
    # 把本次涉及的所有 run 的 jobs.csv 汇总起来
    for rid in runs_seen:
        src = os.path.join(DD, "crawl", "runs", rid, "jobs.csv")
        if not os.path.exists(src):
            continue
        with open(src, encoding="utf-8-sig") as f:
            got = list(csv.DictReader(f))
        log(f"  汇总 run {rid}: {len(got)} 条")
        rows.extend(got)

    # 按 (岗位名, 公司名) 去重，同时用 idmap 把 job_id 还原成岗位链接
    seen, out_rows = set(), []
    for row in rows:
        key = (str(row.get("title", "")).strip(), str(row.get("company", "")).strip())
        if key in seen:
            continue
        seen.add(key)
        jid = idmap.get(key, "")
        out_rows.append({
            "岗位名称": row.get("title", ""),
            "所在城市": row.get("city", "") or city,
            "薪资范围": row.get("salary", ""),
            "公司名称": row.get("company", ""),
            "岗位描述": row.get("post_description", ""),
            "工作经验要求": row.get("experience", ""),
            "学历要求": row.get("education", ""),
            # 拿不到 job_id 时链接留空，不做猜测
            "岗位链接": JOB_URL.format(jid) if jid else "",
            "公司规模": row.get("company_scale", ""),
            "所属行业": row.get("industry", ""),
            "办公地址": row.get("address", ""),
            "技能标签": row.get("labels", ""),
            "福利待遇": row.get("benefits", ""),
        })

    out = os.path.join(OUTDIR, f"boss_{QUERY}_{city}.csv")
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(out_rows)
    # 统计带链接比例：太低通常说明 search 阶段没拿到 job_id（可能部分页失败了）
    hit = sum(1 for r in out_rows if r["岗位链接"])
    log(f"输出 {out} / 共 {len(out_rows)} 条 / 带链接 {hit} 条（{hit * 100 // max(len(out_rows), 1)}%）")


if __name__ == "__main__":
    main()
