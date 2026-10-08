"""清理补抓相关进程（独立脚本，避免命令行自匹配）。

职责
----
把当前正在跑的补抓相关 python 进程（backfill_jd.py / boss_cdp_raw.py）全部 kill 掉，
用于「卡死 / 要重启 / 锁没释放」时的兜底清理。

关键设计决策：为什么关键词要拼接
--------------------------------
KEY_BACKFILL / KEY_SCRAPER 用字符串拼接（"backfill" + "_jd"）而不是直接写完整词，
避免脚本**自己的命令行**匹配到自己（否则清理时可能把执行清理的这个进程也算进去）。
同理只匹配以 `.py` 结尾的 cmdline 参数，避免误杀浏览器等无关进程。

关于「正常进程链」
------------------
一次补抓正常是 **4 个 python 进程的父子链**：
    backfill_jd.py(持锁) → backfill_jd.py → boss_cdp_raw.py → worker
它们属于同一个实例。本脚本会把这 4 个都清掉——这是预期行为，不是误杀。

副作用
------
* **破坏性**：直接 kill 匹配到的进程，未落盘的分块数据可能丢失。
* 清理后 sleep 3 秒再次枚举，打印残留 PID。
* 输出到 stdout。

用法
----
    python cleanup_workers.py
"""

import time

import psutil

KEY_BACKFILL = "backfill" + "_jd"
KEY_SCRAPER = "boss_cdp" + "_raw"


def main():
    """kill 所有补抓相关进程，并复核是否清干净。

    参数：无。
    返回：无。
    副作用：kill 匹配进程；sleep 3 秒；输出到 stdout。
    异常：单个进程的 kill / 读取失败会被就地忽略（可能已自行退出）。
    """
    killed = 0
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            cl = p.info.get("cmdline") or []
            joined = " ".join(cl)
            # 只看命令行里带 .py 的进程，避免误伤其他程序
            if not any(c.lower().endswith(".py") for c in cl):
                continue
            if KEY_BACKFILL in joined or KEY_SCRAPER in joined:
                p.kill()
                killed += 1
                print("killed pid", p.info["pid"])
        except Exception:
            # 进程可能已退出或权限不足，忽略
            pass
    time.sleep(3)  # 等操作系统回收，再复核
    left = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            cl = p.info.get("cmdline") or []
            joined = " ".join(cl)
            # 复核：仍能匹配到 .py + 关键词的，就是残留
            if any(c.lower().endswith(".py") for c in cl) and (
                KEY_BACKFILL in joined or KEY_SCRAPER in joined
            ):
                left.append(p.info["pid"])
        except Exception:
            pass
    print(f"killed={killed} left={left}")


if __name__ == "__main__":
    main()
