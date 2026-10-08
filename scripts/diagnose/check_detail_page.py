"""检查 CDP 浏览器里打开一个职位详情页时能拿到什么（判断是登录墙还是风控）。

职责
----
先经 CDP HTTP 接口列出当前打开的页面；若在命令行给了 URL，
则直接经 WebSocket 发 CDP 命令导航过去，读取 ``document.title`` 与
body 前 600 字，用来判断该页面是登录墙还是风控页。

关键设计决策
------------
* **绕过页面 JS 干扰**：不注入任何脚本，只用 ``Page.navigate`` +
  ``Runtime.evaluate`` 读原始 DOM 文本，避免被站点前端逻辑改写判断。
* 手工实现 send / recv 配对（按 id 匹配响应），不依赖第三方 CDP 封装。

副作用
------
* 会新建 WebSocket 连接并导航被接管的浏览器标签页（改变其当前地址）。
* 只读 DOM，不提交表单。

用法
----
    python check_detail_page.py                 # 只列当前页面
    python check_detail_page.py <职位详情URL>    # 导航并读取标题 / 正文
    # 指定端口：BOSS_CDP_PORT=9223 python check_detail_page.py <URL>
"""

import json
import os
import sys
import urllib.request

CDP = f"http://127.0.0.1:{os.environ.get('BOSS_CDP_PORT', '9222')}"  # 浏览器调试端口
URL = sys.argv[1] if len(sys.argv) > 1 else ""  # 缺省为空表示只列页面，不导航


def main():
    """列出页面；若给了 URL 则导航并打印标题与正文。

    参数：无（URL 取自 sys.argv[1]，为空时表示只列页面）。
    返回：无。
    副作用：可能改变被接管浏览器的当前页面；输出到 stdout。
    异常：CDP 不可达时 urllib 会抛异常（不兜底）。
    """
    with urllib.request.urlopen(CDP + "/json/list", timeout=8) as r:
        tabs = json.load(r)  # CDP 页面列表（每项含 url / type / webSocketDebuggerUrl）
    pages = [t for t in tabs if t.get("type") == "page"]
    print(f"当前页面数: {len(pages)}")
    for t in pages[:8]:  # 最多列 8 个，避免刷屏
        print("  -", (t.get("url") or "")[:110])

    if not URL:
        return  # 没给 URL：只做「列出页面」，到此为止

    from websocket import create_connection  # 局部 import：只在需要导航时才用

    # 找一个可用的 page target（必须有 webSocketDebuggerUrl 才能发命令）
    target = None
    for t in tabs:
        if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
            target = t
            break
    if not target:
        print("没有可用 page target")
        return

    ws = create_connection(target["webSocketDebuggerUrl"], timeout=30)
    mid = [0]  # 用单元素列表做可变计数器（供闭包内递增）

    def send(method, params=None):
        """发一条 CDP 命令并同步等待其响应（按 id 配对），返回响应 dict。"""
        mid[0] += 1
        ws.send(json.dumps({"id": mid[0], "method": method, "params": params or {}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == mid[0]:  # 忽略事件推送，只认本命令的响应
                return msg

    send("Page.enable")
    send("Page.navigate", {"url": URL})  # 触发导航（异步，不等 load 事件）
    import time
    time.sleep(6)  # 固定等 6 秒，给页面加载 / 跳转留时间
    title = send("Runtime.evaluate", {"expression": "document.title", "returnByValue": True})
    body = send("Runtime.evaluate", {"expression": "(document.body.innerText||'').slice(0,600)",
                                     "returnByValue": True})
    # Runtime.evaluate 的返回值层层嵌套：result.result.value 才是真正结果
    print("\n标题:", title.get("result", {}).get("result", {}).get("value"))
    print("正文:", str(body.get("result", {}).get("result", {}).get("value")).replace("\n", " ")[:500])
    ws.close()


if __name__ == "__main__":
    main()
