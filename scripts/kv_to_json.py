"""把「name=value 逐行」格式的 cookie 文本转成 Cookie-Editor JSON。

（用户的 cookie 是从浏览器直接复制的键值对，没有域名信息，统一按 .zhipin.com 处理）

用法：python kv_to_json.py <输入txt> <输出json>

职责
----
把形如「a=1」逐行的纯键值 cookie 文本转成 Cookie-Editor 扩展可导入的 JSON。
因为没有域名/过期信息，统一按 `.zhipin.com` 域、HTTPS、会话 cookie 处理。

关键设计决策
------------
* 用 partition("=") 只切第一个「=」，值里若含「=」不会被截断（走鉴权签名时常见）。
* 只 strip 名称、不 strip 值，避免破坏值末尾的敏感字符。
* 同名 cookie 不去重，全部保留。

副作用
------
* 覆盖写输出 JSON（UTF-8，不转义非 ASCII）。
* 向 stdout 打印条数、cookie 名清单、关键登录态字段是否命中。

用法示例
--------
    python kv_to_json.py cookies.txt cookies_new.json
"""

import json
import sys

DOMAIN = ".zhipin.com"  # 缺省域名：BOSS 直聘主站


def main():
    """读 name=value 文本 → 写 Cookie-Editor JSON（域名统一 .zhipin.com）。

    参数（命令行，均必需）：
        sys.argv[1]: 输入 txt。
        sys.argv[2]: 输出 json。

    跳过规则：空行、以 `#` 开头的行、不含 `=` 的行。

    副作用：
        * 覆盖写输出 JSON（UTF-8，不转义非 ASCII）。
        * 打印条数与 cookie 名清单、关键字段命中情况。

    异常：入参缺失会 IndexError；文件不存在会 FileNotFoundError（均不兜底）。
    """
    src, dst = sys.argv[1], sys.argv[2]
    raw = open(src, encoding="utf-8", errors="replace").read()
    out = []  # 转换后的 cookie 对象列表
    for line in raw.splitlines():
        line = line.strip()
        # 跳过空行 / 注释行 / 不含等号的脏行
        if not line or line.startswith("#") or "=" not in line:
            continue
        # partition 只切第一个 "="，值里再出现 "=" 不会被截断
        name, _, value = line.partition("=")
        name = name.strip()
        if not name:  # 形如 "=value" 的脏行，丢弃
            continue
        out.append({
            "domain": DOMAIN,
            "path": "/",
            "secure": True,    # 统一按 HTTPS 处理
            "httpOnly": False,
            "name": name,
            "value": value,    # 注意：值不做 strip，避免破坏末尾敏感字符
            "sameSite": "unspecified",
            "session": True,   # 无过期信息，一律按会话 cookie
            "storeId": "0",
        })
    json.dump(out, open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"转换 {len(out)} 条 -> {dst}")
    print("cookie:", sorted(c["name"] for c in out))
    # 关键登录态字段自检：wt2 / __zp_stoken__ / bst / zp_at 任一缺失都可能导致注入后未登录
    for k in ("wt2", "__zp_stoken__", "bst", "zp_at"):
        hit = any(c["name"] == k for c in out)
        print(f"  {k}: {'✓' if hit else '✗ 缺失'}")


if __name__ == "__main__":
    main()
