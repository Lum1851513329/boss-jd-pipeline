"""把 Cookie-Editor 导出的 JSON 注入到专用 Chromium profile，并保持浏览器常驻在 CDP 9222。

用法：python inject_cookies.py [cookie文件] [profile目录]
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import (BASE, CHROME_DEFAULT, COOKIE_CHROME, EDGE_DEFAULT,  # noqa: E402
                    KEYWORD, PROFILE_CHROME, PROXY_FILE, SKILL_ROOT)

BASE_DIR = BASE

# 浏览器可切换：BOSS_BROWSER=edge 时用系统 Edge（独立进程、独立 CDP 端口、独立 profile）
BROWSER = os.environ.get("BOSS_BROWSER", "chrome").strip().lower()
CHROME = os.environ.get("BOSS_BROWSER_PATH", EDGE_DEFAULT if BROWSER == "edge" else CHROME_DEFAULT)
BROWSER_EXE = os.path.basename(CHROME)

CDP_PORT = int(os.environ.get("BOSS_CDP_PORT", "9222"))
CDP = f"http://127.0.0.1:{CDP_PORT}"

# 未显式传路径时的默认 cookie 文件：优先 skill 根目录的 cookies.json，
# 其次退回配置里的账号 A cookie。
_local_cookie = os.path.join(SKILL_ROOT, "cookies.json")
DEFAULT_COOKIE = (os.environ.get("BOSS_COOKIE")
                  or (_local_cookie if os.path.exists(_local_cookie) else COOKIE_CHROME))
DEFAULT_PROFILE = os.environ.get("BOSS_PROFILE", PROFILE_CHROME)

# 可选：已知失效的出口 IP（用于判断代理是否真的生效），逗号分隔
BAD_IPS = [x.strip() for x in os.environ.get("BOSS_BAD_IP", "").split(",") if x.strip()]


def get_proxy():
    """代理来源（优先级）：环境变量 BOSS_PROXY > .boss-agent\\proxy.txt。
    格式示例： socks5://127.0.0.1:1080  或  http://user:pass@1.2.3.4:8080
    """
    val = os.environ.get("BOSS_PROXY", "").strip()
    if not val and os.path.exists(PROXY_FILE):
        try:
            val = open(PROXY_FILE, encoding="utf-8").read().strip()
        except Exception:
            val = ""
    return val

SAMESITE = {
    "no_restriction": "None",
    "unspecified": "Lax",
    "lax": "Lax",
    "strict": "Strict",
    "none": "None",
}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def cdp_alive():
    try:
        with urllib.request.urlopen(CDP + "/json/version", timeout=4) as r:
            return r.status == 200
    except Exception:
        return False


def kill_chrome(profile=None):
    """只关闭「本脚本要用的那个」浏览器。

    安全要点：**必须按 --user-data-dir 精确匹配**，否则会把用户自己开着的
    个人浏览器（同样叫 chrome.exe / msedge.exe）一起杀掉 —— 那会丢失用户未保存的标签页。
    匹配不到任何进程时，绝不能退化成"杀掉所有同名进程"；宁可不杀。
    """
    target = os.path.abspath(profile or DEFAULT_PROFILE).lower()
    try:
        import psutil
        n = 0
        for p in psutil.process_iter(["name", "cmdline"]):
            try:
                if (p.info["name"] or "").lower() != BROWSER_EXE.lower():
                    continue
                cl = " ".join(p.info.get("cmdline") or []).lower()
                if target and target in cl:
                    p.kill()
                    n += 1
            except Exception:
                pass
        if n:
            log(f"清理 {n} 个属于本 profile 的 {BROWSER_EXE} 进程")
        else:
            log(f"未发现属于本 profile 的 {BROWSER_EXE} 进程（不动其他浏览器）")
        time.sleep(3)
    except Exception as exc:
        log(f"清理失败: {exc}")


def load_cookies(path, domain_filter="zhipin"):
    raw = json.load(open(path, encoding="utf-8"))
    items = raw if isinstance(raw, list) else (raw.get("cookies") or [])
    out = []
    for c in items:
        dom = str(c.get("domain") or "")
        if domain_filter and domain_filter not in dom.lower():
            continue
        ck = {
            "name": c["name"],
            "value": c["value"],
            "domain": dom,
            "path": c.get("path") or "/",
            "httpOnly": bool(c.get("httpOnly")),
            "secure": bool(c.get("secure", True)),
        }
        ss = SAMESITE.get(str(c.get("sameSite", "")).lower())
        if ss:
            ck["sameSite"] = ss
        exp = c.get("expirationDate")
        if exp:
            ck["expires"] = float(exp)
        out.append(ck)
    return out


def main():
    cookie_file = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_COOKIE
    profile = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_PROFILE
    cookies = load_cookies(cookie_file)
    log(f"载入 {len(cookies)} 条 zhipin cookie")
    names = sorted(c["name"] for c in cookies)
    for key in ("wt2", "__zp_stoken__"):
        log(f"  {key}: {'✓ 存在' if key in names else '✗ 缺失'}")

    kill_chrome(profile)
    os.makedirs(profile, exist_ok=True)

    proxy = get_proxy()
    args = [CHROME, f"--remote-debugging-port={CDP_PORT}", "--disable-http2",
            "--remote-allow-origins=*",
            f"--user-data-dir={profile}", "--no-first-run",
            "--no-default-browser-check", "--window-size=1280,900"]
    log(f"浏览器: {BROWSER} ({BROWSER_EXE}) | CDP 端口 {CDP_PORT}")
    if proxy:
        args.append(f"--proxy-server={proxy}")
        log(f"使用代理: {proxy}")
    else:
        log("未配置代理，走直连")
    args.append("https://www.zhipin.com/")

    log("启动浏览器...")
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=0x00000008 | 0x00000200)
    for _ in range(25):
        time.sleep(2)
        if cdp_alive():
            break
    if not cdp_alive():
        log("浏览器启动失败")
        sys.exit(1)
    log("浏览器就绪，注入 cookie...")

    from patchright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        ctx.add_cookies(cookies)
        log(f"已注入。当前 context cookie 数: {len(ctx.cookies())}")

        page = ctx.new_page()

        # 先用浏览器自己的网络栈确认出口 IP（验证代理是否生效）
        try:
            ip_page = ctx.new_page()
            ip_page.goto("https://api.ipify.org/?format=json", timeout=30000)
            ip_page.wait_for_timeout(1500)
            info = ip_page.inner_text("body")[:300]
            ip_page.close()
            log(f"浏览器出口 IP: {info}")
            for bad in BAD_IPS:
                if bad in info:
                    log(f"⚠️ 出口 IP 仍是已知被封的 {bad}（代理未生效）")
        except Exception as exc:
            log(f"出口 IP 检测失败: {str(exc)[:100]}")

        try:
            import urllib.parse
            probe_url = ("https://www.zhipin.com/web/geek/jobs?query="
                         + urllib.parse.quote(KEYWORD) + "&city=101010100")
            page.goto(probe_url, timeout=60000, wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
            title = page.title()
            body = page.inner_text("body")[:400].replace("\n", " ")
            log(f"页面标题: {title}")
            if "安全验证" in title or "安全验证" in body:
                log("⚠️ 命中安全验证页")
            log(f"正文预览: {body[:200]}")
            html = page.content()
            import re
            k = re.findall(r"\d{1,3}-\d{1,3}K", html)
            log(f"页面中明文薪资样本数: {len(k)}  {k[:6]}")
            if k:
                log("✅ 登录态有效（API 返回明文薪资，方案2 的判定标准）")
            else:
                log("⚠️ 未见明文薪资，可能未登录或被风控")
        except Exception as exc:
            log(f"验证页加载异常: {str(exc)[:180]}")
        log(f"浏览器保持运行（CDP {CDP_PORT}），脚本退出。")


if __name__ == "__main__":
    main()
