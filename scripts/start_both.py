"""双向补抓统一守护：确保两个浏览器都在线，并各自跑一条补抓流水线。

  * 泳道 A：Chrome (CDP 9222 / 实例锁 9333) → forward（从前往后）
  * 泳道 B：Edge   (CDP 9223 / 实例锁 9335) → reverse（从后往前）

两个泳道各用一个**不同的登录账号**（Cookie 与 profile 都在 config.LANES 里配置），
数据源相同、但方向相反，因此不会重复抓同一批岗位。
幂等：已在跑的流水线不会被重启。

用法：python start_both.py [每块条数]

────────────── 在流水线中的位置 ──────────────
这是“双泳道模式”的总入口，通常由计划任务（每小时）反复调用。它负责：
必要时拉起两个 CDP 浏览器、各起一个 backfill_jd.py 子进程，并在启动前
判断该不该跳过（已完成 / 已受限 / 已在跑）。

────────────── 关键设计决策 / 为什么这么做 ──────────────
  * 双向不重叠：forward 从头、reverse 从尾，同一份数据相向而行，
    两条泳道不会抓重复岗位，也就无需跨泳道的去重锁。
  * 幂等守护：每次调用先探状态（lane_pid / restricted），已在跑就什么都不做，
    避免每小时把进行中的分块打断、白抓一遍。
  * 受限即跳过：/passport/ 或 403.html 标签页占多数时视为被封，不启动，
    省得无效重试反而加重风控。

────────────── 重要副作用 ──────────────
  * 会启动子进程：inject_cookies.py（拉浏览器）+ 两个游离的 backfill_jd.py。
  * 会写日志文件：各泳道 lane["log"]（追加写）。
  * 会创建各泳道工作目录 lane["work"]。
  * 不杀任何进程（清理由 start_backfill.py 负责）。

────────────── 用法示例 ──────────────
    python start_both.py          # 每块 120 条（默认）
    python start_both.py 150      # 每块 150 条
"""

import csv
import os
import subprocess
import sys
import time
import urllib.request

# 同 backfill_jd.py：保证能 import 到同目录的 config
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import (BASE, BREAKAWAY, DETACHED, LANES, MASTER,  # noqa: E402
                    MASTER_EDGE, NEWGROUP, PY, SCRIPTS_DIR)

OUTDIR = os.path.dirname(MASTER)  # 预留输出目录（当前未使用）

def log(m):
    """带时间戳的日志；flush 保证被重定向到文件时也能实时看到。"""
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def cdp_alive(port):
    """探测指定 CDP 端口上的浏览器是否在监听。

    参数 port：CDP 调试端口（Chrome 9222 / Edge 9223）。
    返回值：True/False。
    副作用：发一次 HTTP 请求；任何异常（连接被拒、超时）都当作“不在线”。
    """
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=4) as r:
            return r.status == 200
    except Exception:
        return False


def restricted(port):
    """判断该浏览器是否处于「访问受限」（BOSS 临时封禁）状态。

    参数 port：CDP 调试端口。
    返回值：True 表示受限，应跳过本轮。
    判定：所有 page 标签页中，URL 命中 /passport/ 或 403.html 的比例过半（且至少 2 个）。
    阈值取 max(2, len//2)：单个 403 页可能只是正常登录跳转造成的误报，必须至少 2 个才算数。
    副作用：发一次 HTTP 请求；探测失败时返回 False（宁可放行也不误停）。
    """
    try:
        import json
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=6) as r:
            tabs = json.load(r)
    except Exception:
        return False
    pages = [t for t in tabs if t.get("type") == "page"]
    hit = sum(1 for t in pages if "403.html" in str(t.get("url")) or "/passport/" in str(t.get("url")))
    return bool(pages) and hit >= max(2, len(pages) // 2)


def lane_pid(lock):
    """通过泳道的锁端口反查“是否已有实例在跑”。

    backfill_jd.py 用 TCP 端口当实例锁（处于 LISTEN 即持有锁），所以这里扫本机
    TCP 连接，找到监听该端口的进程 pid，就等于找到了正在跑的实例。

    参数 lock：泳道锁端口（forward 9333 / reverse 9335）。
    返回值：持有锁的进程 pid，或 None。
    """
    try:
        import psutil
        for c in psutil.net_connections(kind="tcp"):
            if c.laddr and c.laddr.port == lock and c.status == "LISTEN":
                return c.pid
    except Exception:
        pass
    return None


def progress():
    """汇总两条流水线的 JD 覆盖情况。

    返回值：(have, total) —— 已有 JD 的行数、总行数。
    总行数只取 MASTER（两条泳道行数相同）；覆盖度取两条泳道里更高的那个。
    """
    total = have = 0
    for path in (MASTER, MASTER_EDGE):
        if not os.path.exists(path):
            continue
        rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
        if path == MASTER:
            total = len(rows)  # 以主表行数为准
        # 两条泳道各写各的主表、进度不一，取覆盖度更高的那个代表整体进度
        have = max(have, sum(1 for r in rows if str(r.get("岗位描述", "")).strip()))
    return have, total


def ensure_browser(lane):
    """确保某泳道的浏览器在线，不在就拉起。

    参数 lane：LANES 里的一个泳道配置 dict（含 port/browser/profile/cookie 等）。
    返回值：True 表示最终在线（本就活着或拉起成功），False 表示拉起失败。
    副作用：必要时调用 inject_cookies.py 子进程拉起浏览器（最长等 300s）。
    """
    if cdp_alive(lane["port"]):
        return True
    log(f"[{lane['name']}] 浏览器不在线，拉起 {lane['browser']} ...")
    # 用环境变量告诉 inject_cookies.py 用哪个浏览器 / 端口 / 用户目录
    env = dict(os.environ, PYTHONUTF8="1",
               BOSS_BROWSER=lane["browser"],
               BOSS_CDP_PORT=str(lane["port"]),
               BOSS_PROFILE=lane["profile"])
    subprocess.run([PY, os.path.join(SCRIPTS_DIR, "inject_cookies.py"), lane["cookie"]],
                   env=env, cwd=BASE, timeout=300)
    ok = cdp_alive(lane["port"])
    log(f"[{lane['name']}] 拉起{'成功' if ok else '失败'}")
    return ok


def ensure_lane(lane, chunk):
    """确保某泳道的补抓流水线在跑；已在跑 / 受限则跳过。

    参数 lane：泳道配置 dict。参数 chunk：每块条数，透传给 backfill_jd.py。
    返回值：无。
    副作用：以“脱离会话的游离进程”启动 backfill_jd.py，把它的 stdout/stderr
    追加写到该泳道日志文件；启动后 sleep 4s 等子进程把锁端口占上再返回。
    """
    if lane_pid(lane["lock"]):
        # 锁端口已被监听 = 已有实例在跑，重启只会白打断一次进行中的块
        log(f"[{lane['name']}] 已在运行（锁端口 {lane['lock']}），不重启")
        return
    if restricted(lane["port"]):
        log(f"[{lane['name']}] 处于访问受限状态，跳过")
        return
    os.makedirs(lane["work"], exist_ok=True)
    logfile = open(lane["log"], "a", encoding="utf-8")  # 追加模式，保留历史日志
    # 把该泳道的方向 / 端口 / 锁端口 / 工作目录 / 输出表全塞进环境变量，
    # backfill_jd.py 全靠读这些变量来区分自己是哪条泳道
    env = dict(os.environ, PYTHONUTF8="1",
               BOSS_DIRECTION=lane["direction"],
               BOSS_CDP_PORT=str(lane["port"]),
               BOSS_LOCK_PORT=str(lane["lock"]),
               BOSS_WORK=lane["work"],
               BOSS_OUT=lane["out"],
               BOSS_SRC=MASTER)
    # DETACHED|NEWGROUP|BREAKAWAY：让子进程脱离当前控制台/进程组，
    # 这样本守护进程退出后补抓仍能继续跑
    p = subprocess.Popen([PY, os.path.join(SCRIPTS_DIR, "backfill_jd.py"), chunk],
                         stdout=logfile, stderr=subprocess.STDOUT,
                         env=env, cwd=BASE,
                         creationflags=DETACHED | NEWGROUP | BREAKAWAY)
    time.sleep(4)
    log(f"[{lane['name']}] 已启动 pid={p.pid}")


def main():
    """守一轮：按需拉起两条泳道，然后立刻返回。

    参数：取自 sys.argv —— argv[1] 每块条数（默认 120）。
    返回值：无。
    副作用：可能拉起浏览器与两个游离的 backfill_jd.py 子进程。
    设计为幂等：重复调用不会重复启动，可放心交给计划任务每小时跑一次。
    """
    chunk = sys.argv[1] if len(sys.argv) > 1 else "120"
    have, total = progress()
    log(f"当前进度 {have}/{total} 条带 JD")
    if total and have >= total:
        log("全部完成，无需启动")
        return
    for lane in LANES:
        if ensure_browser(lane):
            ensure_lane(lane, chunk)
    have, total = progress()
    log(f"完成。进度 {have}/{total} 条带 JD")


if __name__ == "__main__":
    main()
