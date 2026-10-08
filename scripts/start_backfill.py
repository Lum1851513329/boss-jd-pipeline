"""守护启动器：确保 CDP 浏览器在线，并保证只有一个 JD 补抓实例在跑。

可反复执行（幂等）：
  * 浏览器不在 9222 → 先跑 inject_cookies.py 拉起
  * 已有存活补抓实例 → 直接退出，不重复启动
  * 否则以独立进程（脱离会话）启动 backfill_jd.py

用法：python start_backfill.py [每块条数]

────────────── 在流水线中的位置 ──────────────
这是“单泳道模式”的守护入口（只用 Chrome / 9222），由计划任务定时调用。
需要 Chrome + Edge 双向并发时改用 start_both.py，二者选其一即可。

────────────── 关键设计决策 / 为什么这么做 ──────────────
  * 启动器自身也加端口锁（9334）：命令被重复执行时只有第一次生效，
    否则可能一次起出两个补抓实例。
  * “健康实例”以 9333 端口是否 LISTEN 为准，而不是数进程个数——
    正常进程链是 4 个 python 的父子链（backfill_jd → backfill_jd →
    boss_cdp_raw → worker），按进程数判断会误判成重复实例。
  * 已受限 / 已完成 / 已有健康实例 → 一律不启动，避免打断进行中的块。

────────────── 重要副作用 ──────────────
  * 会启动子进程：inject_cookies.py（需要时）、游离的 backfill_jd.py。
  * 会杀进程：kill_workers() 会清理旧的 backfill / boss_cdp_raw 进程。
  * 会写日志文件：LOG(backfill.log)，追加模式。

────────────── 用法示例 ──────────────
    python start_backfill.py        # 每块 120 条（默认）
    python start_backfill.py 150    # 每块 150 条
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

# 同 backfill_jd.py：保证能 import 到同目录的 config
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import (BASE, BREAKAWAY, COOKIE_CHROME, DETACHED,  # noqa: E402
                    MASTER, NEWGROUP, PY, SCRIPTS_DIR, WORK)

CDP = "http://127.0.0.1:9222"                 # 单泳道固定使用 Chrome 的 CDP
LOCK = os.path.join(WORK, "_backfill.lock")   # 遗留锁文件路径（锁已改 9333 端口，此处仅作清理）
LOG = os.path.join(WORK, "backfill.log")
COOKIE_FILE = COOKIE_CHROME  # 单泳道模式默认使用的账号

# 用拼接而非字面量 "backfill_jd" 得到待匹配关键字（保持原样，不修改）
KEY = "backfill" + "_jd"


def log(m):
    """带时间戳的日志；flush 保证被重定向到文件时也能实时看到。"""
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def cdp_alive():
    """探测 9222 上的 Chrome CDP 是否在线。

    返回值：True/False；任何异常（连接被拒、超时）一律按不在线处理。
    """
    try:
        with urllib.request.urlopen(CDP + "/json/version", timeout=4) as r:
            return r.status == 200
    except Exception:
        return False


def running_pids():
    """列出命令行里带 backfill_jd 关键字的 python 进程 pid。

    仅用于日志展示（启动后打印一下存活进程），不参与任何判断逻辑。
    返回值：pid 列表。
    """
    import psutil
    out = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            cl = p.info.get("cmdline") or []
            if any(c.lower().endswith(".py") and KEY in c for c in cl):
                out.append(p.info["pid"])
        except Exception:
            pass
    return out


def restricted():
    """检测浏览器是否处于「访问受限 / 403」状态（BOSS 临时封禁）。

    返回值：True 表示受限（本轮不启动）。
    判定：page 标签页中 URL 命中 /passport/ 或 403.html 的占比过半（且至少 2 个）。
    阈值 max(2, total//2)：要求至少 2 个命中，避开单标签页登录跳转造成的误报。
    探测失败时返回 False——宁可放行也不误停整条流水线。
    """
    try:
        with urllib.request.urlopen(CDP + "/json/list", timeout=6) as r:
            tabs = json.load(r)
    except Exception:
        return False
    hit = 0
    total = 0
    for t in tabs:
        if t.get("type") != "page":
            continue
        total += 1
        u = str(t.get("url") or "")
        if "403.html" in u or "/passport/" in u:
            hit += 1
    if total and hit >= max(2, total // 2):
        log(f"检测到访问受限：{hit}/{total} 个标签页是 403 页")
        return True
    return False


def progress():
    """读取主表，统计 JD 覆盖度。

    返回值：(have, total) —— 已有 JD 的行数、总行数；主表不存在时返回 (0, 0)。
    副作用：读主表文件。注意这里只看主表本身，不含 save() 退写出的时间戳变体。
    """
    import csv
    if not os.path.exists(MASTER):
        return 0, 0
    rows = list(csv.DictReader(open(MASTER, encoding="utf-8-sig")))
    have = sum(1 for r in rows if str(r.get("岗位描述", "")).strip())
    return have, len(rows)


def worker_running():
    """通过端口 9333 判断是否已有健康实例在跑（该端口是 backfill_jd 的实例锁）。

    为什么按端口而不是按进程数：正常进程链是一串 4 个 python 的父子进程
    （backfill_jd → backfill_jd → boss_cdp_raw → worker），数进程会误判成多实例；
    端口处于 LISTEN 才是“锁被持有”的确凿信号。

    返回值：持有锁的进程 pid，或 None（没有实例在跑）。
    """
    try:
        import psutil
        for c in psutil.net_connections(kind="tcp"):
            if c.laddr and c.laddr.port == 9333 and c.status == "LISTEN":
                return c.pid
    except Exception:
        pass
    return None


def kill_workers():
    """清理所有 backfill / scraper 进程（仅在确认没有健康实例时才调用）。

    只有 worker_running() 判定无健康实例时才会走到这里，用来清掉残留的进程链。
    注意：它会杀掉正在跑的块、打断进行中的抓取——但此场景本就没有健康实例，
    且 backfill_jd 的恢复池（absorb_existing）会在下次启动时把已抓到的 JD 收回，
    所以不会丢数据。
    返回值：无。副作用：杀进程；杀到东西后 sleep 4s 等端口/文件句柄释放。
    """
    import psutil
    killed = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            cl = p.info.get("cmdline") or []
            if not any(c.lower().endswith(".py") for c in cl):
                continue  # 只动 python 进程，避免误伤浏览器等
            joined = " ".join(cl)
            if KEY in joined or ("boss_cdp" + "_raw") in joined:
                p.kill()
                killed.append(p.info["pid"])
        except Exception:
            pass
    if killed:
        log(f"已清理旧进程 {killed}")
        time.sleep(4)


def main():
    """守一轮：按需拉起浏览器与补抓进程，然后返回。

    参数：取自 sys.argv —— argv[1] 每块条数（默认 120）。
    返回值：无。
    副作用：可能启动 inject_cookies.py / backfill_jd.py 子进程，可能杀旧进程。
    设计为可反复执行（幂等）。
    """
    chunk = sys.argv[1] if len(sys.argv) > 1 else "120"
    os.makedirs(WORK, exist_ok=True)

    # 启动器自身也加端口锁：命令被重复执行时，只有第一次生效
    import socket

    launcher_lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # 关键：Windows 默认允许重复绑定同一端口，必须声明独占，否则启动器被重复执行时会起两个补抓实例
    try:
        launcher_lock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    except (AttributeError, OSError):
        pass
    try:
        launcher_lock.bind(("127.0.0.1", 9334))
        launcher_lock.listen(1)
    except OSError:
        log("另一个启动器正在运行，本次退出")
        return

    have, total = progress()
    log(f"进度 {have}/{total} 条带 JD")
    if total and have >= total:
        log("全部已完成，无需启动")
        return

    if not cdp_alive():
        log("浏览器不在线，拉起...")
        subprocess.run([PY, os.path.join(SCRIPTS_DIR, "inject_cookies.py"), COOKIE_FILE],
                       cwd=BASE, timeout=300)
        if not cdp_alive():
            log("浏览器拉起失败，放弃本次启动")
            return
    log("浏览器在线")

    if restricted():
        log("当前处于访问受限状态，本次不启动补抓（等待限制解除）")
        return

    # 已有健康实例在跑 → 不打扰（避免每小时把进行中的分块打断、白抓一遍）
    live = worker_running()
    if live:
        log(f"已有健康实例在运行（pid {live}），本次不重启")
        return

    kill_workers()
    if os.path.exists(LOCK):
        try:
            os.remove(LOCK)  # 顺手清遗留锁文件；本机 safe-delete 若失败会被 except 吞掉
        except Exception:
            pass

    logfile = open(LOG, "a", encoding="utf-8")  # 追加模式，保留历史日志
    # DETACHED|NEWGROUP|BREAKAWAY：让子进程脱离当前会话，守护进程退出后补抓仍继续
    p = subprocess.Popen([PY, os.path.join(SCRIPTS_DIR, "backfill_jd.py"), chunk],
                         stdout=logfile, stderr=subprocess.STDOUT,
                         env=dict(os.environ, PYTHONUTF8="1"), cwd=BASE,
                         creationflags=DETACHED | NEWGROUP | BREAKAWAY)
    time.sleep(5)
    log(f"已启动独立补抓进程 pid={p.pid}；存活={running_pids()}")


if __name__ == "__main__":
    main()
