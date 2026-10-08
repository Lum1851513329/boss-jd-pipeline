"""批量补抓 JD（岗位描述），合并回主表。

要点：
  * 源表只读（避免被预览面板占用导致写失败），结果写到新文件
  * 启动时自动吸收 _JD补抓/out_*.json 里已抓到的 JD（自带恢复能力）
  * 分块处理，每块立即落盘 → 可中断续跑

用法：python backfill_jd.py [每块条数] [总条数上限]

────────────── 在流水线中的位置 ──────────────
采集分两段：第一段由 boss_cdp_raw.py 抓岗位「列表」，第二段就是本脚本，
逐条补抓详情页里的「JD 正文」。本脚本通常不手跑，而是由 start_backfill.py
或 start_both.py 守护拉起，并支持 Chrome(forward) / Edge(reverse) 两条泳道并发。

────────────── 关键设计决策 / 为什么这么做 ──────────────
  * 单实例用 TCP 端口锁，不用锁文件：本机 safe-delete 会让 os.remove() 失败，
    锁文件删不掉会永久残留；TCP 端口由 OS 在进程退出时自动释放（见 acquire_lock）。
  * 并发不靠加锁靠「约定」：同一条泳道可能同时有两个实例，因此所有共享文件名
    都带 pid（in_*/out_*/进度文件/tmp）。历史上仅因文件名共享就崩过三次主循环。
  * 块级认领：并发实例用 claim_*.lock 认领不同的块，各做各的，互不重复抓。
  * 恢复优先：启动先 absorb_existing，把历史 out_*.json 里的 JD 收回来，
    所以进程被杀/崩溃都不丢数据，重启即续跑——这是「可中断续跑」的核心。
  * 稳健节奏：块间休息 BLOCK_REST（默认 240s），牺牲速度换低风控概率。

────────────── 重要副作用 ──────────────
  * 会启动子进程：每块调用一次 SCRAPER(boss_cdp_raw.py)，单块最长 4 小时。
  * 会写文件：主表 OUT（写失败时退写成 <OUT>_<时间戳>.csv）、
    in_*.json / out_*.json、进度文件 _jd_done_<dir>_<port>.json.<pid>。
  * 不主动杀任何进程。

────────────── 用法示例 ──────────────
    python backfill_jd.py              # 每块 150 条、不限总量
    python backfill_jd.py 120 3000     # 每块 120 条、本轮最多处理 3000 条
"""

import csv
import glob
import json
import os
import subprocess
import sys

import psutil
import time

# 让脚本无论从哪个目录被调起，都能 import 到同目录的 config（也避免被同名包抢占）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import (BASE, BLOCK_REST, DATA_DIR, FIELDS, MASTER, PY,  # noqa: E402
                    SCRAPER, SRC, WORK)

OUTDIR = DATA_DIR                           # 预留输出目录（当前流程未使用，仅为配置一致性保留）
OUT = MASTER                                # 主表（默认写目标）；可被 BOSS_OUT 覆盖
TRACK = os.path.join(WORK, "_jd_done.json")  # 进度文件初始路径，下面按泳道重算

# ---- 多浏览器 / 双向分工配置（用环境变量驱动）----
# BOSS_DIRECTION: forward（从头往后）| reverse（从后往前）
# 通过环境变量拿到本泳道的方向、CDP 端口、锁端口、工作目录与输出表，
# 于是同一份代码能被 Chrome / Edge 两条泳道各自独立拉起。
DIRECTION = os.environ.get("BOSS_DIRECTION", "forward").strip().lower()
CDP_PORT = os.environ.get("BOSS_CDP_PORT", "9222").strip()
LOCK_PORT = int(os.environ.get("BOSS_LOCK_PORT", "9333"))
if os.environ.get("BOSS_WORK"):
    WORK = os.environ["BOSS_WORK"].strip()   # 两条泳道各用独立工作目录，避免共享文件名
if os.environ.get("BOSS_OUT"):
    OUT = os.environ["BOSS_OUT"].strip()     # 两条泳道各写各的主表
# 进度文件名带方向+端口：不同泳道/实例互不覆盖
TRACK = os.path.join(WORK, f"_jd_done_{DIRECTION}_{CDP_PORT}.json")


def log(m):
    """带时间戳的日志。

    参数 m：要打印的消息。
    返回值：无。
    副作用：打印到 stdout。flush=True 是必须的——本脚本的 stdout 常被守护进程
    重定向到日志文件，若不强制刷新，缓冲会让日志长时间不落盘，无法实时排障。
    """
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


# 遗留锁文件路径（见下）与端口锁 socket 的模块级引用
LOCK = WORK + r"\_backfill.lock"
_LOCK_SOCK = None  # 持有监听 socket，防止被 GC 回收后端口被提前释放


def acquire_lock():
    """单实例仲裁：占用本地 TCP 端口当锁。

    为什么不用锁文件：本机 safe-delete 策略会让 os.remove() 失败，锁文件删不掉、永久残留。
    TCP 端口由操作系统管理，进程退出自动释放，最可靠。

    参数：无（端口取自模块级 LOCK_PORT，由 BOSS_LOCK_PORT 决定，默认 9333）。
    返回值：无。
    副作用：成功则占用端口并打印日志；失败（端口被占）调用 sys.exit(0)。
    注意退出码用 0：这属于“已有实例在跑”的正常情况，不该让守护脚本判为失败。
    """
    import socket

    global _LOCK_SOCK
    if _LOCK_SOCK is not None:
        return  # 已持有锁，重入直接返回（供主循环异常重试时复用）
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Windows 默认允许重复绑定同一端口，必须显式声明独占，否则锁形同虚设
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    except (AttributeError, OSError):
        pass  # 非 Windows 平台没有该选项，忽略即可
    try:
        # bind 即“抢锁”：成功说明没有别的实例占着；失败说明锁已被持有
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
        _LOCK_SOCK = s  # 保持引用，进程存活期间不释放
        log(f"实例锁已获取（127.0.0.1:{LOCK_PORT} 独占），pid={os.getpid()}")
    except OSError:
        log(f"端口 {LOCK_PORT} 已被占用——已有补抓实例在运行，本次退出")
        sys.exit(0)


def claim_batch(bi):
    """认领一个批次：并发实例各做不同的批次，避免重复抓同一批岗位。

    用「认领文件 + 记录 pid」实现：
      - 文件已存在且记录的 pid 还活着 → 已被别人认领，返回 False
      - 文件已存在但 pid 已死（陈旧认领）→ 覆盖成本进程的 pid，返回 True
      - 文件不存在 → 原子创建，返回 True
    注意：本机 safe-delete 会阻止 os.remove，所以这里只创建/覆盖、从不删除。

    参数 bi：批次起始下标（不是批次序号），用于拼认领文件名 claim_<bi:05d>.lock。
    返回值：True 表示本进程成功认领该块、可以去做；False 表示跳过。
    副作用：可能创建或覆盖一个 claim_*.lock 文件。
    """
    path = os.path.join(WORK, f"claim_{bi:05d}.lock")
    mine = os.getpid()
    if os.path.exists(path):
        try:
            # 只读不删：认出文件里记的 pid 是否还活着
            if psutil.pid_exists(int(open(path, encoding="utf-8").read().strip())):
                return False
        except Exception:
            # 文件存在但内容读不出/不是数字：保守当作“别人在用”，跳过
            return False
        # 走到这里说明是陈旧认领（原 pid 已死），覆盖成本进程的 pid
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(str(mine))
            return True
        except Exception:
            return False
    try:
        # O_CREAT|O_EXCL 是原子“仅当不存在才创建”，两个实例同时抢时只有一个能成功
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(mine).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False  # 恰好被别人抢先创建
    except Exception:
        # 其他异常（如路径不可写）宁可放行继续抓，也不阻塞流水线
        return True


    # TODO: 以下 atexit 清理逻辑位于上一段 return 之后，永远不会被执行（死代码）。
    # 它引用的是早已废弃的锁文件 LOCK；本机 safe-delete 也删不掉该文件。
    # 属疑似遗留代码，按“只加注释不改逻辑”的约定保留原样，不在此处修改。
    import atexit

    def _cleanup():
        try:
            if os.path.exists(LOCK) and open(LOCK, encoding="utf-8").read().strip() == str(os.getpid()):
                os.remove(LOCK)
        except Exception:
            pass

    atexit.register(_cleanup)


def save(rows):
    """把内存中的 rows 写回主表 OUT。

    参数 rows：list[dict]，键与 FIELDS 对应；只写 FIELDS 里的列（extrasaction="ignore"）。
    返回值：无。
    副作用：写主表；失败时可能落成 <OUT>_<时间戳>.csv（见下方退写逻辑）。
    写法是“先写 tmp 再 os.replace”的原子替换——目标要么是旧内容、要么是新内容，
    不会出现写一半的半成品；tmp 名带 pid，避免两个实例的 tmp 互相覆盖。
    """
    # tmp 名字带 pid，避免同名实例互抢
    tmp = f"{OUT}.{os.getpid()}.tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    # 并发实例可能短暂占用目标文件，重试几次再退回带时间戳的文件名
    for attempt in range(5):
        try:
            os.replace(tmp, OUT)
            return
        except PermissionError:
            # 目标被预览面板 / 另一个实例占用：退避重试（依次等 1,2,3,4,5 秒）
            time.sleep(1.0 + attempt)
        except Exception as exc:
            log(f"  ⚠️ 主表写入异常: {str(exc)[:80]}")
            break
    try:
        # 退写：宁可落成带时间戳的旁路文件，也不丢这批数据。
        # 后果是“数据已落盘但主表没更新”，所以统计进度时必须 glob 所有时间戳变体。
        alt = OUT.replace(".csv", f"_{int(time.time())}.csv")
        os.replace(tmp, alt)
        log(f"  ⚠️ 主表被占用，已改写 {os.path.basename(alt)}")
    except Exception as exc:
        log(f"  ⚠️ 主表写入失败（不影响抓取）: {str(exc)[:80]}")


def absorb_existing(rows, by_id):
    """把 _JD补抓/out_*.json 里已抓到的 JD 合并进来。

    这是“可中断续跑”的关键：进程被杀 / 崩溃 / 块被 kill 打断后重启时，
    靠它把上一轮已经抓到的 JD 从历史输出文件里收回来，因此不重复抓、也不丢数据。

    参数 rows：完整行列表（本函数当前只读 by_id，rows 参数保留但未使用）。
    参数 by_id：job_id → 行 dict 的索引；命中后直接就地写入该行的「岗位描述」。
    返回值：本次新吸收到的 JD 条数 got。
    副作用：就地修改 by_id 中各行的「岗位描述」字段。
    """
    got = 0
    for f in sorted(glob.glob(os.path.join(WORK, "out_*.json"))):
        try:
            for d in json.load(open(f, encoding="utf-8")):
                jid = d.get("job_id")
                jd = str(d.get("jd") or "").strip()
                # 只补“原本为空”的行：已有 JD 的不覆盖，避免用劣质结果冲掉好结果
                if jid in by_id and jd and not str(by_id[jid].get("岗位描述", "")).strip():
                    by_id[jid]["岗位描述"] = jd
                    got += 1
        except Exception as exc:
            # 单个文件损坏（如半写）不该影响整体恢复，跳过即可
            log(f"  跳过 {os.path.basename(f)}: {str(exc)[:60]}")
    if got:
        log(f"恢复已抓 JD {got} 条")
    return got


def main():
    """跑一轮完整的补抓流程，执行完即返回。

    参数：取自 sys.argv —— argv[1] 每块条数（默认 150），argv[2] 总条数上限（默认不限）。
    返回值：无。
    副作用（重要）：
      * 每块启动一个 scraper 子进程（单块 timeout=14400s）；
      * 写 in_*.json / out_*.json / 进度文件 / 主表；
      * 命中风控关键词时 sleep 900s，块间 sleep BLOCK_REST；
      * 端口锁被占时经 acquire_lock 直接 sys.exit(0)。
    """
    chunk = int(sys.argv[1]) if len(sys.argv) > 1 else 150  # 每块条数
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 10 ** 9  # 总条数上限，默认近似不限
    acquire_lock()  # 拿不到实例锁会直接退出，防止同一泳道重复跑
    os.makedirs(WORK, exist_ok=True)

    # 读取哪张表：BOSS_SRC 优先（多浏览器分工时，两边都读同一张"最新主表"）
    src = os.environ.get("BOSS_SRC", "").strip()
    if not src:
        src = OUT if os.path.exists(OUT) else SRC
    rows = list(csv.DictReader(open(src, encoding="utf-8-sig")))
    for r in rows:
        r.setdefault("岗位描述", "")  # 源表可能缺这一列，先兜底，后面才好按列取值
    by_id = {r["job_id"]: r for r in rows}  # job_id 索引，供块结果回填
    log(f"源表 {src} → {len(rows)} 条")

    absorb_existing(rows, by_id)
    have = sum(1 for r in rows if str(r.get("岗位描述", "")).strip())
    # 待补 = 有链接、且 JD 仍为空的行
    todo = [r for r in rows if str(r.get("岗位链接", "")).strip()
            and not str(r.get("岗位描述", "")).strip()]
    if DIRECTION == "reverse":
        # 反向泳道从尾部开始抓：与 forward 泳道相向而行，天然不重叠
        todo.reverse()
        log(f"方向=reverse（从后往前，CDP {CDP_PORT}），共 {len(todo)} 条待补")
    log(f"已有 JD {have} 条 / 待补 {len(todo)} 条")
    if not todo:
        save(rows)
        log("无需补抓")
        return

    t0 = time.time()
    processed = 0
    per = 8.0  # 每条耗时估计（秒）；第一块拿到实测值后会被覆盖，仅作初始占位

    for bi in range(0, min(len(todo), limit), chunk):
        # 并发实例各认领不同批次，避免两个实例重复抓同一批岗位
        if not claim_batch(bi):
            log(f"  第 {bi // chunk + 1} 块已被其他实例认领，跳过")
            continue
        batch = todo[bi:bi + chunk]
        # 输入/输出文件名都带 pid：同一泳道的并发实例各写各的文件。
        # 本机锁不住并发，只要文件名共享就随时可能因 PermissionError 崩掉主循环。
        _me = os.getpid()
        inp = os.path.join(WORK, f"in_{bi:05d}_{_me}.json")
        outp = os.path.join(WORK, f"out_{bi:05d}_{_me}.json")
        json.dump({"keyword": "AI产品经理", "city": "ALL", "total": len(batch),
                   "jobs": [{"job_id": r["job_id"], "title": r["岗位名称"],
                             "boss_name": r["公司名称"], "salary": r["薪资范围"],
                             "location": r["所在城市"] + "·" + r.get("区域", ""),
                             "tags": r["工作经验要求"] + " | " + r["学历要求"],
                             "job_link": r["岗位链接"]} for r in batch]},
                  open(inp, "w", encoding="utf-8"), ensure_ascii=False)

        log(f"=== 第 {bi // chunk + 1} 块：{len(batch)} 条（{DIRECTION} / CDP {CDP_PORT}）===")
        # PYTHONUTF8=1：强制子进程用 UTF-8，避免中文 JD 在 GBK 控制台下乱码/报错
        env = dict(os.environ, PYTHONUTF8="1")
        # 每块起一个 scraper 子进程；timeout=14400（4h）是单块上限，防止它卡死拖垮整轮
        p = subprocess.run([PY, SCRAPER, "--input", inp, "--detail",
                            "--max-details", str(len(batch)),
                            "--cdp-port", CDP_PORT,
                            "--detail-output", outp],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=14400)
        # 只留日志尾部几百字符做风控判断，全量太大没必要
        tail = ((p.stdout or "") + (p.stderr or ""))[-400:]
        blocked = any(k in tail for k in ("访问受限", "暂时无法访问", "异常行为"))
        if any(k in tail for k in ("安全验证", "环境存在异常", "code 37", "code 31")) or blocked:
            # 疑似风控：先长睡 15 分钟再继续，别硬刚加重风控
            log(f"  ⚠️ 疑似风控/受限，暂停 15 分钟。尾部: {tail[-160:]}")
            time.sleep(900)
        if blocked:
            # 明确“访问受限”则整轮停止：继续跑只会无谓失败，还加重封禁
            log("  检测到「访问受限」，立即停止本轮，等待解封后再跑")
            save(rows)
            break

        got = 0
        if os.path.exists(outp):
            try:
                for d in json.load(open(outp, encoding="utf-8")):
                    jid, jd = d.get("job_id"), str(d.get("jd") or "").strip()
                    if jid in by_id and jd:
                        by_id[jid]["岗位描述"] = jd
                        got += 1
            except Exception as exc:
                log(f"  读取块输出失败: {str(exc)[:80]}")
        else:
            # 子进程没产出（崩溃 / 被杀 / 卡超时），把尾部日志发出来便于定位
            log(f"  未产出结果: {tail[-200:]}")

        processed += len(batch)
        if got:
            # 用实测速度刷新“每条耗时”，让剩余时间估计更准
            per = (time.time() - t0) / processed
        if got < len(batch) * 0.6:
            log(f"  本块产出偏低（{got}/{len(batch)}），尾部日志: {tail[-260:].replace(chr(10), ' | ')}")
        have = sum(1 for r in rows if str(r.get("岗位描述", "")).strip())
        save(rows)
        # 进度文件也必须带 pid：并发实例写同一个文件会 PermissionError，导致进程崩溃
        try:
            tmp = f"{TRACK}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump([r["job_id"] for r in rows if str(r.get("岗位描述", "")).strip()],
                          fh, ensure_ascii=False)
            os.replace(tmp, f"{TRACK}.{os.getpid()}")
        except Exception as exc:
            # 进度文件只是辅助信息，写不了就算了，绝不能让它拖垮抓取主循环
            log(f"  ⚠️ 进度文件写入失败（忽略，不影响抓取）: {str(exc)[:80]}")

        el = time.time() - t0
        log(f"  本块 +{got} / 已有 JD {have}/{len(rows)} | 已用 {el / 60:.0f} 分钟 | {per:.1f}s/条")
        log(f"  预计剩余 {(len(todo) - processed) * per / 3600:.1f} 小时")

        if bi + chunk < min(len(todo), limit):
            # 只要后面还有块就休息一次：稳健优先，降低风控概率
            log(f"  块间休息 {BLOCK_REST}s（稳健节奏，降低风控概率）")
            time.sleep(BLOCK_REST)

    save(rows)
    have = sum(1 for r in rows if str(r.get("岗位描述", "")).strip())
    log("=" * 56)
    log(f"结束。{len(rows)} 条中 {have} 条带 JD → {OUT}")


if __name__ == "__main__":
    # 主循环兜底：本机并发环境复杂（文件抢占、CDP 抖动），
    # 任何未预期异常都不应该让补抓进程退出——记录后继续。
    while True:
        try:
            main()
            break
        except SystemExit:
            raise  # acquire_lock 的 sys.exit(0) 属正常退出，要原样透传而不是被吞掉
        except Exception as exc:
            log(f"⚠️ 主循环异常，10 秒后自动继续：{type(exc).__name__}: {str(exc)[:140]}")
            time.sleep(10)
