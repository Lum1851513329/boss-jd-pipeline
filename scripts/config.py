# -*- coding: utf-8 -*-
"""统一配置层：路径 / 解释器 / CDP 端口 / 账号 / 数据集命名。

设计原则
--------
* **所有值都能用环境变量覆盖**，不改代码即可换机器、换关键词、换数据集。
* 脚本之间共享的常量只在这里定义一次，避免"改了 A 忘了 B"。
* **不硬编码任何绝对路径**：`BASE` 依次从 `BOSS_BASE` 环境变量、
  skill 目录下的 `.boss-base` 文件、当前工作目录 三处回退解析
  （见 `_default_base()`），所以同一份代码在任何机器上都能跑。

常用环境变量
------------
    BOSS_BASE       工作区根目录        （默认见 _default_base()）
    BOSS_DATA_DIR   数据目录            （默认 <BASE>/岗位数据）
    BOSS_PY         Python 解释器        （默认自动探测）
    BOSS_KEYWORD    采集关键词           （默认 AI产品经理）
    BOSS_DATASET    数据集名             （默认 AI产品经理_全量数据）
    BOSS_PROXY      代理，如 socks5://127.0.0.1:1080
    BOSS_BROWSER    chrome | edge
    BOSS_CDP_PORT   浏览器调试端口
    BOSS_PROFILE    浏览器 profile 目录
    BOSS_DIRECTION  forward | reverse
    BOSS_LOCK_PORT  补抓实例锁端口
    BOSS_WORK       单条泳道的工作目录
    BOSS_OUT        单条泳道的输出 CSV
    BOSS_SRC        读取的源表
    BOSS_BROWSER_PATH  浏览器可执行文件路径
"""

import os
import sys

# ---------------------------------------------------------------- 基础路径
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))   # <skill>/scripts
SKILL_ROOT = os.path.dirname(SCRIPTS_DIR)                  # <skill>
SCRAPER_DIR = os.path.join(SKILL_ROOT, "scraper")
SKILL_DIR = SCRIPTS_DIR  # 兼容别名

# 工作区根目录：默认取当前项目，可用 BOSS_BASE 覆盖
def _default_base():
    """解析工作区根目录（数据目录、`.boss-agent`、浏览器 profile 都挂在它下面）。

    优先级（依次回退，任何一步命中就返回）：

    1. 环境变量 ``BOSS_BASE`` —— 推荐做法，换机器/换项目只需设这一个变量。
    2. skill 目录下的 ``.boss-base`` 文件（单行路径）—— 用于「本地已固定好、
       不想每次敲环境变量」的场景；该文件已进 ``.gitignore``，不会被提交。
    3. 当前工作目录 —— 兜底。**注意**：此时数据目录默认落在
       ``<cwd>/岗位数据``，所以请在项目根目录下运行脚本。
    """
    env = os.environ.get("BOSS_BASE")
    if env:
        return env

    marker = os.path.join(SKILL_ROOT, ".boss-base")
    if os.path.exists(marker):
        try:
            v = open(marker, encoding="utf-8").read().strip()
            if v:
                return v
        except Exception:
            pass

    return os.getcwd()


BASE = _default_base()

# ---------------------------------------------------------------- 解释器与浏览器
# 为什么子进程的 Python 必须单独解析：父进程会用它去拉起 backfill_jd.py /
# boss_cdp_raw.py，而那两个需要 patchright / psutil / websocket-client / openpyxl。
# 直接用 sys.executable 有可能拿到一个没装依赖的解释器。
HOME = os.environ.get("USERPROFILE") or os.path.expanduser("~")


def _pick_python():
    """挑一个依赖齐全的 Python 解释器给子进程用。

    优先级：
      1. ``BOSS_PY`` 环境变量（最可靠，建议在 CI / 定时任务里显式设置）
      2. 工作区里的虚拟环境 ``.venv`` / ``venv``
      3. WorkBuddy 托管的隔离环境 ``~/.workbuddy/binaries/python/envs/boss-agent``
      4. 当前解释器所在目录的同名解释器
      5. 当前解释器（兜底）
    """
    env = os.environ.get("BOSS_PY")
    if env:
        return env
    for cand in (
        os.path.join(BASE, ".venv", "Scripts", "python.exe"),
        os.path.join(BASE, "venv", "Scripts", "python.exe"),
        os.path.join(HOME, ".workbuddy", "binaries", "python", "envs",
                     "boss-agent", "Scripts", "python.exe"),
        os.path.join(os.path.dirname(sys.executable), "python.exe"),
        sys.executable,
    ):
        if cand and os.path.exists(cand):
            return cand
    return sys.executable


PY = _pick_python()

# 上游抓取内核（eatmoreduck/boss-zhipin-scraper, MIT）
SCRAPER = os.path.join(SCRAPER_DIR, "boss_cdp_raw.py")

# ---------------------------------------------------------------- 数据集
KEYWORD = os.environ.get("BOSS_KEYWORD", "AI产品经理")
DATASET = os.environ.get("BOSS_DATASET", "AI产品经理_全量数据")

DATA_DIR = os.environ.get("BOSS_DATA_DIR") or os.path.join(BASE, "岗位数据")

SRC = os.path.join(DATA_DIR, f"{DATASET}.csv")                 # 列表主表（可能无 JD）
MASTER = os.path.join(DATA_DIR, f"{DATASET}_含JD.csv")          # forward 主表
MASTER_EDGE = os.path.join(DATA_DIR, f"{DATASET}_含JD_edge.csv")  # reverse 主表
XLSX = os.path.join(DATA_DIR, f"{DATASET}_含JD.xlsx")

WORK = os.path.join(DATA_DIR, "_JD补抓")          # forward 工作目录
WORK_EDGE = os.path.join(DATA_DIR, "_JD补抓_edge")  # reverse 工作目录

# ---------------------------------------------------------------- 账号 / 浏览器
AGENT_DIR = os.path.join(BASE, ".boss-agent")
COOKIE_CHROME = os.path.join(AGENT_DIR, "cookies_new.json")       # 泳道 A 的账号
COOKIE_EDGE = os.path.join(AGENT_DIR, "cookies_liyoutian.json")   # 泳道 B 的账号
PROFILE_CHROME = os.path.join(AGENT_DIR, "cdp-profile")
PROFILE_EDGE = os.path.join(AGENT_DIR, "edge-profile")
PROXY_FILE = os.path.join(AGENT_DIR, "proxy.txt")

# 浏览器可执行文件。可用 BOSS_BROWSER_PATH 覆盖；Edge 是系统安装路径，
# Chrome 默认指向 Playwright 自带的 Chromium（路径里带版本号，故用通配探测）。
EDGE_DEFAULT = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"


def _pick_chrome():
    """探测 Playwright 自带 Chromium 的可执行文件路径（目录名含版本号，需 glob）。"""
    import glob
    pats = [
        os.path.join(HOME, "AppData", "Local", "ms-playwright", "chromium-*",
                     "chrome-win64", "chrome.exe"),
        os.path.join(HOME, "AppData", "Local", "ms-playwright", "chromium-*",
                     "chrome-win", "chrome.exe"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for p in pats:
        hit = sorted(glob.glob(p))
        if hit:
            return hit[-1]
    return "chrome.exe"  # 交给 PATH 解析


CHROME_DEFAULT = _pick_chrome()

# 双泳道定义：不同浏览器 + 不同账号 + 相反方向 → 天然不重复抓同一批岗位
LANES = [
    {
        "name": "Chrome/forward",
        "browser": "chrome", "port": 9222, "lock": 9333,
        "profile": PROFILE_CHROME, "cookie": COOKIE_CHROME,
        "work": WORK, "out": MASTER, "direction": "forward",
        "log": os.path.join(WORK, "backfill.log"),
    },
    {
        "name": "Edge/reverse",
        "browser": "edge", "port": 9223, "lock": 9335,
        "profile": PROFILE_EDGE, "cookie": COOKIE_EDGE,
        "work": WORK_EDGE, "out": MASTER_EDGE, "direction": "reverse",
        "log": os.path.join(WORK_EDGE, "backfill_edge.log"),
    },
]

# ---------------------------------------------------------------- 表结构
FIELDS = [
    "job_id", "岗位名称", "所在城市", "区域", "薪资范围", "公司名称", "岗位描述",
    "工作经验要求", "学历要求", "岗位链接", "公司规模", "融资阶段",
    "所属行业", "技能标签", "福利待遇", "Boss活跃度", "薪资来源",
]
# 不含 JD 的列表字段（矩阵采集阶段用）
FIELDS_LIST = [f for f in FIELDS if f != "岗位描述"]

# ---------------------------------------------------------------- 抓取节奏
BLOCK_REST = int(os.environ.get("BOSS_BLOCK_REST", "240"))  # 块间休息秒数
CHUNK = int(os.environ.get("BOSS_CHUNK", "120"))             # 每块条数

# 受限/风控关键词：命中即认为被拦，不要重试
BLOCK_KEYWORDS = ("访问受限", "暂时无法访问", "异常行为", "安全验证",
                  "请稍后再试", "验证码", "code 37", "code 31", "环境存在异常")

# ---------------------------------------------------------------- Windows 进程标志
DETACHED = 0x00000008
NEWGROUP = 0x00000200
BREAKAWAY = 0x01000000
