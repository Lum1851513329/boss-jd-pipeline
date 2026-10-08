"""探测本机常见的代理端口，找出可用并显示其出口 IP。

职责
----
遍历常见本地代理端口（Clash / V2Ray / 系统代理等），对每个端口分别以
HTTP 和 SOCKS5 各试一次请求 ``http://ip-api.com/json/``，
打印成功端口的出口 IP 归属，用于确认代理是否真的生效（出口 IP 是否变了）。

关键设计决策
------------
* 先 HTTP 后 SOCKS5：多数本地代理（如 Clash 7890）两者都支持，HTTP 判定更快。
* 用 ip-api.com 的返回值直接看出口 IP / 国家 / 城市，一眼判断是否走了代理。
* 端口列表覆盖 Clash(7890/7891/7897/7899…)、V2Ray(10809/10808)、
  通用 http 代理(8080/3128/8118) 等常见组合。

副作用
------
* 会向外部服务 ip-api.com 发请求（取决于代理是否可用）。
* 只输出到 stdout，不落盘、不改配置。

用法
----
    python probe_proxy.py
"""

import urllib.request

PORTS = [7890, 7891, 7897, 7899, 1080, 1081, 10809, 10808, 1087, 8889,
         8118, 20171, 33210, 12639, 2080, 9090, 7078, 8888, 8080, 3128]

TEST = "http://ip-api.com/json/?lang=zh-CN"  # 返回出口 IP 及其地理位置


def try_port(port, scheme="http"):
    """用指定协议试连 127.0.0.1:<port> 代理并访问测试地址。

    参数：
        port:   本地端口号。
        scheme: "http" 或 "socks5"。

    返回：
        (ok, info)。成功时 info 是响应正文前 220 字符；失败时是异常信息前 70 字符。

    副作用：发起一次网络请求（8 秒超时）。
    """
    proxy = f"{scheme}://127.0.0.1:{port}"
    handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    opener = urllib.request.build_opener(handler)
    try:
        with opener.open(TEST, timeout=8) as r:
            body = r.read().decode("utf-8", "replace")[:220]
        return True, body
    except Exception as exc:
        return False, str(exc)[:70]


def main():
    """遍历候选端口，打印可用代理及其出口信息。

    参数：无。
    返回：无。
    副作用：多次网络探测；输出到 stdout。
    """
    print("探测本机代理端口...\n")
    found = []  # [(端口, 协议), ...]
    for p in PORTS:
        ok, info = try_port(p)
        if ok:
            print(f"[HTTP] 端口 {p} 可用 -> {info}")
            found.append((p, "http"))
        else:
            # HTTP 不通再试 SOCKS5（有些端口只开 SOCKS）
            ok2, info2 = try_port(p, "socks5")
            if ok2:
                print(f"[SOCKS5] 端口 {p} 可用 -> {info2}")
                found.append((p, "socks5"))
    if not found:
        print("未发现可用代理端口（可能代理只在 IPv4/系统级别生效，或端口不在常见列表里）")
    else:
        print("\n可用代理:")
        for p, s in found:
            print(f"  {s}://127.0.0.1:{p}")


if __name__ == "__main__":
    main()
