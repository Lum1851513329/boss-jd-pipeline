"""把 Netscape 格式的 cookie 文件转成 Cookie-Editor JSON（供 inject_cookies.py 使用）。

用法：python netscape_to_json.py <输入txt> <输出json>

职责
----
把浏览器/工具导出的 Netscape cookies.txt（制表符分隔的 7 列）转成
Cookie-Editor 扩展能导入的 JSON 数组，供后续注入到 CDP 浏览器。

格式说明（Netscape cookies.txt，每行 7 列，\t 分隔）
--------------------------------------------------
    domain  flag  path  secure  expires  name  value

关键设计决策
------------
* 只认 7 列齐全的行，列数不足直接跳过。
* expires 能被解析且 >0 才写 expirationDate，否则标记为会话 cookie（session=True）。
* 输出用 indent=1 + ensure_ascii=False，便于人工查看中文/特殊字符。

副作用
------
* 覆盖写输出 JSON 文件。
* 向 stdout 打印转换条数、cookie 名称清单，以及关键登录态字段是否命中。

用法示例
--------
    python netscape_to_json.py cookies.txt cookies_new.json
"""

import json
import sys


def main():
    """执行转换：读 Netscape txt → 写 Cookie-Editor JSON。

    参数（命令行，均必需）：
        sys.argv[1]: 输入 txt 路径。
        sys.argv[2]: 输出 json 路径。

    返回值：无（恒为 None）。

    副作用：
        * 覆盖写输出 JSON（UTF-8，不转义非 ASCII）。
        * 打印条数、cookie 名清单、关键字段命中情况。

    异常：入参缺失会 IndexError；输入文件不存在会 FileNotFoundError（均不兜底）。
    """
    src = sys.argv[1]
    dst = sys.argv[2]
    out = []  # 转换后的 cookie 对象列表
    for line in open(src, encoding="utf-8", errors="replace"):
        # 去掉行尾换行（兼容 \r\n）
        line = line.rstrip("\n").rstrip("\r")
        if not line:
            continue

        # Netscape 的扩展约定：httpOnly 的 cookie 行以 `#HttpOnly_` 开头，
        # 真正的 domain 紧跟在该前缀之后。
        # 必须先识别这个前缀再判"注释行"，否则会把这些行当注释丢掉 ——
        # 而登录态 cookie（wt2 / __zp_stoken__）常常正是 httpOnly 的，
        # 丢了就会出现"注入成功但仍是未登录"的现象。
        http_only = False
        if line.startswith("#HttpOnly_"):
            http_only = True
            line = line[len("#HttpOnly_"):]

        # 其余以 # 开头的才是真注释行
        if line.startswith("#"):
            continue

        parts = line.split("\t")  # Netscape 用制表符分隔
        if len(parts) < 7:         # 列数不足 7 视为脏行
            continue
        # 7 列依次为：domain、flag、path、secure、expires、name、value。
        # flag 列用 `_` 占位（标记 httpOnly 与否），这里不关心，绑定给 _flag 丢弃。
        domain, _flag, path, secure, expires, name, value = parts[:7]
        out.append({
            "domain": domain,
            "path": path or "/",                        # path 缺失时补 "/"
            "secure": str(secure).upper() == "TRUE",    # 字符串 "TRUE" 才视为安全
            "httpOnly": http_only,                      # 由 `#HttpOnly_` 前缀解析得到
            # expires 是 Unix 时间戳；非数字或 <=0 时写 None（表示会话 cookie）
            "expirationDate": int(expires) if expires.isdigit() and int(expires) > 0 else None,
            "name": name,
            "value": value,
            "sameSite": "unspecified",
            "session": not (expires.isdigit() and int(expires) > 0),  # 与 expirationDate 互补
            "storeId": "0",
        })
    json.dump(out, open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"转换 {len(out)} 条 cookie -> {dst}")
    print("cookie 名称:", sorted(c["name"] for c in out))
    # 关键登录态字段自检：wt2 / __zp_stoken__ / bst 任一缺失都可能导致注入后未登录
    for key in ("wt2", "__zp_stoken__", "bst"):
        hit = any(c["name"] == key for c in out)
        print(f"  {key}: {'✓' if hit else '✗ 缺失'}")


if __name__ == "__main__":
    main()
