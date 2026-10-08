"""按「先取链接、再单独采集、风控则冷却续采」策略采集单个城市。

流水线位置
==========
属于**采集列表 + JD**阶段。与 collect_city.py 同属 boss-agent CLI 路线，
但把顺序换成「先把所有页的链接取全，再一口气采集列表 + JD」——
好处是采集阶段拿到的 job_id 映射已经完整，输出 CSV 里岗位链接的命中率更高。

阶段划分（见 main）：
  阶段一 phase_links  分页 search，攒 (岗位名, 公司名) -> job_id 映射
  阶段二 phase_crawl  crawl run/resume，抓列表 + JD
  阶段三              合并两个来源，输出单城市 CSV

关键经验：
  * BOSS 会对无头/独立 profile 浏览器做风控 => 必须复用带界面的已登录浏览器
  * crawl 必须与 search 分开跑，且每轮前重启浏览器，否则采集页会拿到空结果
  * resume 遵守创建时的 --pages 上限，所以一次 run 就开到目标页数，被打断后再 resume
  * crawl 会把浏览器关掉，所以每轮前都要确保 9222 上有浏览器

重要副作用
----------
  * 通过 subprocess 调用外部 CLI `boss.exe`（pip 包 boss_agent_cli）。
  * **会 kill 掉机器上所有 chrome.exe 进程**（kill_chrome，见该函数注释）。
  * 链接映射落盘在 `<DD>/tmp/links_<城市>.json`（可续用）。
  * 采集结果由 CLI 写在 `<DD>/crawl/runs/<run_id>/jobs.csv`。
  * 最终输出 `<DATA_DIR>/boss_<关键词>_<城市>.csv`。

用法：python collect2.py <城市> [页数] [冷却秒数]
  * 城市      必填
  * 页数      默认 10（同时是 phase_links 的页数和 crawl 的 --pages）
  * 冷却秒数  默认 480

环境变量: BOSS_CLI / BOSS_BROWSER_PATH / BOSS_CDP_PORT 可分别覆盖 CLI、
浏览器路径与 CDP 端口。
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

BOSS = os.environ.get("BOSS_CLI") or os.path.join(
    os.path.dirname(sys.executable), "boss.exe")
CHROME = os.environ.get("BOSS_BROWSER_PATH") or CHROME_DEFAULT
DD = AGENT_DIR
PROFILE = PROFILE_CHROME
OUTDIR = DATA_DIR
LINKDIR = os.path.join(DD, "tmp")  # 链接映射的落盘目录（可续用）
CDP_PORT = int(os.environ.get("BOSS_CDP_PORT", "9222"))
CDP = f"http://127.0.0.1:{CDP_PORT}"

QUERY = KEYWORD
JOB_URL = "https://www.zhipin.com/job_detail/{}.html"
CDP_ARGS = ["--cdp-url", CDP, "--browser-source", "existing-browser"]

# BOSS 的城市用数字码表示（URL query 里的 city= 参数）。
# 搜索接口用的是城市名，而拉起浏览器时得用这里的数字码拼 URL，故需要这张表；
# 未知城市一律退回北京 101010100。
CITY_CODE = {"北京": "101010100", "上海": "101020100", "深圳": "101280600",
             "杭州": "101210100", "苏州": "101190400"}

FIELDS = [
    "岗位名称", "所在城市", "薪资范围", "公司名称", "岗位描述",
    "工作经验要求", "学历要求", "岗位链接",
    "公司规模", "所属行业", "办公地址", "技能标签", "福利待遇",
]


def log(msg):
    """带时间戳打印一行进度日志。

    参数 msg: 要打印的文本。flush=True 保证重定向到文件时也实时可见。
    """
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def boss(args, timeout=3600):
    """调用外部 CLI `boss.exe` 并解析它的 JSON 结果。

    参数:
      args    : 子命令与参数（会自动补上 --json / --data-dir / CDP 相关参数）
      timeout : 子进程超时秒数，默认 3600（本文件的 crawl 都是长任务）

    返回: 解析出的 dict（约定含 ok / error / data）。
    解析失败返回 {"ok": False, "error": {"code": "NO_JSON", ...}} 而不抛异常。

    坑：`--json` 模式仍会打印进度条等杂输出，所以从后往前找第一行
    以 `{` 开头的行去 json.loads，而不是直接解析整个 stdout。
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


def kill_chrome():
    """关掉**属于本流水线 profile** 的 chrome.exe 进程，然后等 3 秒。

    为什么要清理：crawl 结束后浏览器状态可能已经脏了，继续用同一个实例采集
    容易拿到空结果，所以每轮采集前把它清掉再重新拉起。

    安全要点（重要）
    ----------------
    只杀**命令行里带本项目 `--user-data-dir=<PROFILE>`** 的进程。

    早期版本是按进程名匹配的「全局清理」，会把用户自己开着的 Chrome
    （同样叫 chrome.exe）一起杀掉 —— 那会丢失用户未保存的标签页，
    属于不可接受的数据损失。现在匹配不到就**什么都不杀**，
    绝不允许退化成"杀掉所有同名进程"。

    副作用：杀进程 + sleep 3。psutil 不可用或调用失败时只记一行日志、不抛异常。
    """
    target = os.path.abspath(PROFILE).lower()
    try:
        import psutil
        n = 0
        for proc in psutil.process_iter(["name", "cmdline"]):
            try:
                if (proc.info["name"] or "").lower() != "chrome.exe":
                    continue
                # 用 --user-data-dir 精确判定归属，避免误杀个人浏览器
                cl = " ".join(proc.info.get("cmdline") or []).lower()
                if target and target in cl:
                    proc.kill()
                    n += 1
            except Exception:
                pass  # 进程可能刚好自己退了，忽略个体失败
        if n:
            log(f"  已清理 {n} 个属于本 profile 的浏览器进程")
        else:
            log("  未发现属于本 profile 的浏览器进程（不动其他浏览器）")
        time.sleep(3)
    except Exception as exc:
        log(f"  清理进程失败: {exc}")


def cdp_alive():
    """探测 CDP 端口上是否有可用的浏览器（HTTP 200 即在线）。

    返回: True / False；任何异常都当不在线。
    """
    try:
        with urllib.request.urlopen(CDP + "/json/version", timeout=4) as r:
            return r.status == 200
    except Exception:
        return False


def ensure_browser(city):
    """确保 CDP 端口上有浏览器；没有就按城市的数字码拉起一个。

    参数 city: 城市名，用来查 CITY_CODE 拼开屏 URL（只影响打开哪个页面）。
    返回: True 就绪 / False 拉起失败。

    副作用: 可能启动带远程调试端口的浏览器进程，最多阻塞约 40 + 3 秒。
    用的是已登录的 PROFILE，绝不能换成干净 profile（会被风控）。
    """
    if cdp_alive():
        return True
    code = CITY_CODE.get(city, "101010100")
    import urllib.parse
    url = ("https://www.zhipin.com/web/geek/jobs?query="
           + urllib.parse.quote(QUERY) + f"&city={code}")
    log("  拉起浏览器...")
    # --disable-http2 绕开 BOSS 在 HTTP/2 下的连接异常；
    # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP 让浏览器脱离本进程独立存活
    subprocess.Popen([CHROME, f"--remote-debugging-port={CDP_PORT}", "--disable-http2",
                      f"--user-data-dir={PROFILE}", "--no-first-run",
                      "--no-default-browser-check", "--window-size=1200,900", url],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=0x00000008 | 0x00000200)
    for _ in range(20):
        time.sleep(2)
        if cdp_alive():
            time.sleep(3)  # 端口通了不等于页面就绪，再稳 3 秒
            log("  浏览器就绪")
            return True
    log("  浏览器拉起失败")
    return False


def is_risk(res):
    """判断 CLI 返回结果是否为风控/被拦。

    参数 res: boss() 返回的 dict。
    返回: True 表示疑似风控（错误码含 "37"，或信息含 风控/安全验证/异常）。
    """
    code = str((res.get("error") or {}).get("code") or "")
    msg = str((res.get("error") or {}).get("message") or "")
    return "37" in code or "风控" in msg or "安全验证" in msg or "异常" in msg


def phase_links(city, pages, cooldown):
    """阶段一：分页搜索，拿 (岗位,公司) -> job_id。逐页落盘，风控则按冷却时长等待。

    参数:
      city     : 城市名
      pages    : 搜索结果要翻到第几页
      cooldown : 命中风控后的冷却秒数

    返回: idmap 字典，键 (岗位名, 公司名)，值 job_id。

    副作用: 每页都写一次 `<LINKDIR>/links_<城市>.json`（中途被打断也不丢进度）；
    可能拉起浏览器、阻塞 sleep。

    续跑: 启动时若已有 links 文件就先读进来，本次只补没拿到的页。
    说明: search 不返回岗位链接，只给 job_id，所以这里必须先存映射，
    阶段三再用它把 job_id 拼成链接。
    """
    os.makedirs(LINKDIR, exist_ok=True)
    path = os.path.join(LINKDIR, f"links_{city}.json")
    idmap = {}
    if os.path.exists(path):
        try:
            # 磁盘上把 (岗位,公司) 用 \u0001 拼成一个 key 存（JSON 的 key 必须唯一且可字符串化）；
            # 读回来时再按 \u0001 切回二元组。用 \u0001 是因为岗位名/公司名里不会出现这个字符。
            idmap = {tuple(k.split("\u0001", 1)): v
                     for k, v in json.load(open(path, encoding="utf-8")).items()}
            log(f"  复用已有链接映射 {len(idmap)} 条")
        except Exception:
            idmap = {}

    def save():
        """把当前 idmap 落盘（元组键用 \u0001 拼成字符串 key）。"""
        json.dump({"\u0001".join(k): v for k, v in idmap.items()},
                  open(path, "w", encoding="utf-8"), ensure_ascii=False)

    consecutive_fail = 0
    for pg in range(1, pages + 1):
        got = False
        for attempt in range(2):
            ensure_browser(city)
            res = boss(["search", QUERY, "--city", city, "--page", str(pg), "--no-cache"], timeout=300)
            if res.get("ok"):
                jobs = res.get("data") or []
                for j in jobs:
                    key = (str(j.get("title", "")).strip(), str(j.get("company", "")).strip())
                    idmap[key] = j.get("job_id") or ""
                log(f"  search p{pg}: +{len(jobs)} / 累计 {len(idmap)}")
                got = True
                break
            if is_risk(res):
                # 风控：按传入的冷却时长等待，然后用掉第二次 attempt 重试
                log(f"  search p{pg} 风控，冷却 {cooldown}s 后重试")
                time.sleep(cooldown)
                continue
            log(f"  search p{pg} 失败: {((res.get('error') or {}).get('message') or '')[:110]}")
            time.sleep(15)
        save()  # 无论成功与否都落盘，保证进度不丢
        consecutive_fail = 0 if got else consecutive_fail + 1
        if consecutive_fail >= 2:
            # 连续两页都没结果，多半是账号/IP 已被盯上，继续翻页没意义
            log(f"  连续 {consecutive_fail} 页失败，结束取链接阶段（已拿 {len(idmap)} 条）")
            break
        time.sleep(2.5)  # 页间停顿，降低风控概率
    save()
    return idmap


def phase_crawl(city, pages, cooldown):
    """阶段二：单独跑采集，风控则冷却后 resume。

    参数:
      city     : 城市名
      pages    : 目标页数，直接作为 crawl run 创建时的 --pages 上限
      cooldown : 未达页数时，resume 前的冷却秒数

    返回: run_id（采集失败则为 None，调用方据此判失败）。

    副作用: 先 kill_chrome 清场再拉起浏览器；由 CLI 在
    `<DD>/crawl/runs/<run_id>/jobs.csv` 落盘结果；最多 resume 4 次、阻塞 sleep。

    关键: resume 不会提高创建时的 --pages 上限，所以这里一次性按 pages 开 run，
    之后靠 resume 把没跑完的部分续上，而不是每轮新建一个 run。
    """
    kill_chrome()  # 清掉脏实例，避免采集页拿到空结果
    ensure_browser(city)
    res = boss(["crawl", "run", QUERY, "--city", city, "--pages", str(pages), "--with-detail"], timeout=5400)
    run_id = None
    if res.get("ok") and res.get("data"):
        d = res["data"]
        run_id = d.get("run_id")
        log(f"  crawl: pages={d.get('pages_completed')}/{pages} jobs={d.get('jobs_seen')} "
            f"details={d.get('detail_checks')} status={d.get('status')} {d.get('elapsed_seconds')}s")
        if d.get("error"):
            # 部分失败时 CLI 会在 data.error 里带提示，但仍有产出，不当作整体失败
            log(f"  crawl 内部提示: {str(d['error'])[:160]}")
        done = d.get("pages_completed") or 0
    else:
        log(f"  crawl 调用失败: {((res.get('error') or {}).get('message') or '')[:160]}")
        done = 0

    attempts = 0
    # 没跑够页数就冷却后 resume，最多 4 次，避免无限重试把自己拖死
    while run_id and done < pages and attempts < 4:
        attempts += 1
        log(f"  未达 {pages} 页（当前 {done}），冷却 {cooldown}s 后 resume（第 {attempts} 次）")
        time.sleep(cooldown)
        kill_chrome()  # resume 前同样先清场、再重新拉起浏览器
        ensure_browser(city)
        res = boss(["crawl", "resume", run_id], timeout=5400)
        if res.get("ok") and res.get("data"):
            d = res["data"]
            # 取 max：resume 偶尔会报出比上次更小的 pages_completed，别把进度改回去
            done = max(done, d.get("pages_completed") or 0)
            log(f"  resume: pages={d.get('pages_completed')}/{pages} jobs={d.get('jobs_seen')} "
                f"details={d.get('detail_checks')} status={d.get('status')}")
        else:
            log(f"  resume 失败: {((res.get('error') or {}).get('message') or '')[:160]}")
    return run_id


def main():
    """入口：串起三个阶段，输出单城市 CSV。

    位置参数: argv[1]=城市（必填）, argv[2]=页数(10), argv[3]=冷却秒数(480)。

    副作用: 拉起/杀掉浏览器、写 links JSON、写最终 CSV；
    没有任何采集结果时 sys.exit(1)。
    """
    city = sys.argv[1]
    pages = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    cooldown = int(sys.argv[3]) if len(sys.argv) > 3 else 480
    os.makedirs(OUTDIR, exist_ok=True)

    log(f"########## [{city}] 阶段一：搜索取链接（{pages} 页）##########")
    idmap = phase_links(city, pages, cooldown)

    log(f"########## [{city}] 阶段二：采集列表 + JD ##########")
    run_id = phase_crawl(city, pages, cooldown)
    if not run_id:
        log("没有采集结果")
        sys.exit(1)

    log(f"########## [{city}] 阶段三：合并输出 ##########")
    src = os.path.join(DD, "crawl", "runs", run_id, "jobs.csv")
    with open(src, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    log(f"  run {run_id} 共 {len(rows)} 条")

    # 按 (岗位名, 公司名) 去重，并用阶段一的映射补回岗位链接
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
            # 映射里没有就留空，不猜测链接
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
    # 带链接比例过低 => 阶段一的链接映射没匹配上，值得排查
    hit = sum(1 for r in out_rows if r["岗位链接"])
    log(f"输出 {out} / 共 {len(out_rows)} 条 / 带链接 {hit} 条（{hit * 100 // max(len(out_rows), 1)}%）")


if __name__ == "__main__":
    main()
