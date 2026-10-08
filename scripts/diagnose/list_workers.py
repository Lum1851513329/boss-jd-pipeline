"""列出补抓相关进程及其启动时间（用拼接关键词，避免自匹配）。

职责
----
枚举本机 python 进程，挑出与补抓流水线相关的（backfill_jd.py / boss_cdp_raw.py），
打印 PID 与已运行时长，用于判断「是否有实例在跑」「跑了多久」。

关键设计决策：为什么关键词要拼接
--------------------------------
KEY_BACKFILL / KEY_SCRAPER 用字符串拼接（"backfill" + "_jd"）而不是直接写完整词，
是为了避免脚本**自己的命令行**匹配到自己——拼接写法让源码里不出现完整关键词。
同理，只匹配以 `.py` 结尾的 cmdline 参数，进一步排除无关的浏览器/系统进程。

关于「正常进程链」
------------------
一次补抓正常是 **4 个 python 进程的父子链**：
    backfill_jd.py(持锁) → backfill_jd.py → boss_cdp_raw.py → worker
它们属于**同一个实例**，不是重复实例。判断是不是新实例，要看谁持有锁端口
（9333 / 9335）——本脚本只负责列出进程，不判锁。

副作用
------
* 只读：仅枚举进程，不修改任何状态。
* 输出到 stdout。

用法
----
    python list_workers.py
"""

import time

import psutil

KEY_BACKFILL = "backfill" + "_jd"
KEY_SCRAPER = "boss_cdp" + "_raw"


def main():
    """枚举进程并打印补抓相关的 PID / 运行时长。

    参数：无。
    返回：无。
    副作用：只读枚举；输出到 stdout。
    异常：单个进程的 cmdline / create_time 读取失败会被就地忽略（常见于刚退出的进程）。
    """
    now = time.time()
    # 只取需要的最小字段（pid / cmdline / create_time），减少单进程读取失败的概率
    for p in psutil.process_iter(["pid", "cmdline", "create_time"]):
        try:
            cl = p.info.get("cmdline") or []
            # 只看命令行里带 .py 的进程，排除无关的系统进程
            if not any(c.lower().endswith(".py") for c in cl):
                continue
            joined = " ".join(cl)
            tag = None
            if KEY_BACKFILL in joined:
                tag = "backfill"
            elif KEY_SCRAPER in joined:
                tag = "scraper"
            if tag:
                age = int(now - p.info["create_time"])  # 已运行秒数
                print(f"[{tag}] pid={p.info['pid']} 已运行 {age // 60}分{age % 60}秒")
        except Exception:
            # 进程可能在枚举途中退出，此时取字段会抛异常，忽略即可
            pass


if __name__ == "__main__":
    main()
