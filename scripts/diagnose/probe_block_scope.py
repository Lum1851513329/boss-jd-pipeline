"""对照实验：用全新无 Cookie 的浏览器上下文访问 BOSS，判断封禁是 IP 级还是账号级。

职责 / 判定逻辑
--------------
在同一台机器、同一出口 IP 下做两组对照：

* 匿名组：``browser.new_context()`` 全新上下文，无任何 Cookie；
* 已登录组：复用 ``browser.contexts[0]``（CDP 里已登录的那个上下文）。

各自访问一次关键词搜索页，看正文里有没有「访问受限」「异常行为」：

* 匿名正常 + 已登录被拦 → **账号级软风控**（会跳 ``/passport/`` 安全验证页，
  人工滑一下人机验证即可恢复，不用等）；
* 两组都被拦            → **IP 级封禁**（显示恢复时刻，只能等解封，别重试）。

关键设计决策
------------
* 用 CDP ``connect_over_cdp`` 接管已开浏览器，直接复用其登录态做对照，
  不必重新登录。
* 只取 body 前 260 字符判断，足够覆盖拦截页文案。

副作用
------
* 会在被接管的浏览器里新开 / 关闭标签页。
* 只读页面内容，不提交任何表单。

用法
----
    python probe_block_scope.py
    # 指定端口：BOSS_CDP_PORT=9223 python probe_block_scope.py
"""

import os
import sys
import urllib.parse

from patchright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import KEYWORD  # noqa: E402

CDP = f"http://127.0.0.1:{os.environ.get('BOSS_CDP_PORT', '9222')}"  # 被接管的浏览器调试端口
URL = ("https://www.zhipin.com/web/geek/jobs?query="
       + urllib.parse.quote(KEYWORD) + "&city=101010100")  # 101010100 = 北京


def probe(label, ctx):
    """在给定浏览器上下文里访问搜索页，打印标题 / 正文并判定是否被拦。

    参数：
        label: 打印用标签（如「匿名 无Cookie」）。
        ctx:   patchright 的 BrowserContext。

    返回：无（结果只打印）。

    副作用：新开一个 page，结束后关闭。
    异常：导航 / 取值异常被捕获打印，不会向外抛出。
    """
    page = ctx.new_page()
    try:
        page.goto(URL, timeout=45000, wait_until="domcontentloaded")
        page.wait_for_timeout(3500)  # 等 JS 渲染 / 拦截跳转完成
        title = page.title()
        body = page.inner_text("body")[:260].replace("\n", " ")  # 压成单行便于打印比对
        print(f"\n[{label}] 标题: {title}")
        print(f"[{label}] 正文: {body}")
        # 命中拦截文案即判「被拦」；否则视为正常
        if "访问受限" in body or "异常行为" in body:
            print(f"[{label}] 结论: ❌ 被拦")
        else:
            print(f"[{label}] 结论: ✅ 正常访问")
    except Exception as exc:
        print(f"[{label}] 异常: {str(exc)[:150]}")
    finally:
        page.close()  # 无论成功失败都关页，避免残留标签


def main():
    """跑匿名 / 已登录两轮对照并各自给出结论。

    参数：无。
    副作用：接管 CDP 浏览器，新开 / 关闭标签页；输出到 stdout。
    """
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP)

        # 1) 匿名上下文（无 Cookie）
        # locale / timezone 设成中文 / 上海，尽量与真实环境一致，减少地域性差异
        anon = browser.new_context(locale="zh-CN", timezone_id="Asia/Shanghai")
        probe("匿名 无Cookie", anon)
        anon.close()

        # 2) 已登录上下文（带 Cookie）做对照
        # contexts[0] 是 CDP 里现成的用户上下文；没有就跳过（说明浏览器没开登录态）
        if browser.contexts:
            probe("已登录 带Cookie", browser.contexts[0])


if __name__ == "__main__":
    main()
