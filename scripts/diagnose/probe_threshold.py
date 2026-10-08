"""探测当前账号/环境的风控阈值：连续翻页搜索，看第几页开始被拦。

用法：python probe_threshold.py [城市] [最大页数] [页间隔秒]

职责
----
调用外部 `boss` CLI 逐页拉搜索结果（`--no-cache` 强制走网络），
记录成功页数、总条数、首次失败页，用来估算「连翻多少页会触发风控」。

关键设计决策
------------
* **连续 3 次失败就停**：判定式 `pg - (first_fail or pg) >= 2`——
  首次失败后再最多试 2 页就收手，避免探测本身把风控踩得更严重。
* 页间隔默认 5 秒，可命令行覆盖，用来模拟不同节奏。
* 结尾按「每页耗时 + 8 分钟冷却」估算可持续的页 / 小时上限。

副作用
------
* 会真实请求 BOSS 搜索接口（产生流量、可能触发风控）。
* 只写 stdout 日志，不落盘数据。

用法示例
--------
    python probe_threshold.py 北京 15 5
"""

import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import AGENT_DIR, KEYWORD  # noqa: E402

BOSS = os.environ.get("BOSS_CLI") or os.path.join(
    os.path.dirname(sys.executable), "boss.exe")           # 外部 boss CLI 可执行文件
DD = AGENT_DIR                                             # CLI 的 --data-dir
CDP_PORT = os.environ.get("BOSS_CDP_PORT", "9222")
CDP_ARGS = ["--cdp-url", f"http://127.0.0.1:{CDP_PORT}",
            "--browser-source", "existing-browser"]        # 复用已开浏览器
QUERY = KEYWORD


def log(m):
    """带时间戳打印一行日志并立即 flush（便于实时观察长任务）。"""
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def boss(args, timeout=300):
    """调用外部 boss CLI 并解析其 JSON 输出。

    参数：
        args:    传给 CLI 的子命令与参数列表（如 ["search", QUERY, ...]）。
        timeout: 子进程超时秒数，默认 300。

    返回：
        CLI 输出的最后一个 JSON 对象（dict）；解析不到时返回形如
        ``{"ok": False, "error": {"code": "NO_JSON", ...}, "data": None}`` 的占位字典。

    副作用：启动子进程，继承环境变量（额外设 PYTHONUTF8=1 保证中文编码）。
    """
    env = dict(os.environ, PYTHONUTF8="1")  # 强制子进程 UTF-8，避免 Windows 中文乱码
    p = subprocess.run([BOSS, "--json", "--data-dir", DD] + CDP_ARGS + args,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env, timeout=timeout)
    # 从后往前找第一行以 { 开头的输出：CLI 可能在 JSON 之前打印了日志行
    for line in reversed((p.stdout or "").strip().splitlines()):
        if line.strip().startswith("{"):
            try:
                return json.loads(line)
            except Exception:
                pass
    # 一行都解析不出来：返回带错误码的占位结果，交给上层当「失败」处理
    return {"ok": False, "error": {"code": "NO_JSON",
            "message": ((p.stdout or "") + (p.stderr or ""))[-160:]}, "data": None}


def main():
    """按参数逐页探测风控阈值并输出统计。

    参数（命令行，均可选）：
        argv[1] 城市，默认「北京」；
        argv[2] 最大页数，默认 15；
        argv[3] 页间隔秒，默认 5.0。

    副作用：真实请求搜索接口；输出日志到 stdout。
    """
    city = sys.argv[1] if len(sys.argv) > 1 else "北京"
    maxp = int(sys.argv[2]) if len(sys.argv) > 2 else 15
    gap = float(sys.argv[3]) if len(sys.argv) > 3 else 5.0
    t0 = time.time()
    ok_pages, total, first_fail = 0, 0, None
    seen = set()  # 去重后的 job_id 集合，衡量真实产出
    for pg in range(1, maxp + 1):
        r = boss(["search", QUERY, "--city", city, "--page", str(pg), "--no-cache"])
        if r.get("ok"):
            jobs = r.get("data") or []
            for j in jobs:
                seen.add(j.get("job_id"))
            ok_pages += 1
            total += len(jobs)
            log(f"p{pg}: +{len(jobs)} 条 (累计 {total}, 去重 {len(seen)})")
        else:
            code = (r.get("error") or {}).get("code")
            log(f"p{pg}: 失败 code={code} :: {str((r.get('error') or {}).get('message'))[:90]}")
            if first_fail is None:
                first_fail = pg  # 记住首次失败页
            # 连续 3 次失败就停
            # 例：first_fail=7 时，pg=7、8 继续，pg=9 满足 9-7>=2 后 break
            if pg - (first_fail or pg) >= 2:
                break
        time.sleep(gap)
    dur = time.time() - t0
    log("=" * 54)
    log(f"结果：成功 {ok_pages} 页 / 共 {total} 条 / 去重后 {len(seen)} 个岗位")
    log(f"首次失败页：{first_fail if first_fail else '无（未触发风控）'}")
    log(f"耗时 {dur:.0f}s，平均 {dur / max(ok_pages, 1):.1f}s/页")
    if ok_pages:
        rate = len(seen) / ok_pages  # 平均每页去重产出（当前公式未使用该值，保留原样）
        # 估算：把每页耗时 + 8 分钟(480s)冷却算进去，得到「可持续页/小时」上限
        log(f"按此速率 + 8 分钟冷却：约 {int(3600 / (ok_pages * (dur / ok_pages + 480) / 3600))} 页/小时 ≈ 上限")


if __name__ == "__main__":
    main()
