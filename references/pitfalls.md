# 踩坑清单

这些坑都是在这套流水线真实跑了几十万次请求后踩出来的。**动手改代码前先读完**，
能省掉几个小时的排查。

---

## 1. 进度判断：不要看主表条数

### 1.1 主表会被"退写成时间戳文件"

`backfill_jd.py` 的 `save()`：写 tmp → `os.replace(tmp, OUT)`。若目标被占用（预览面板、
另一个实例），重试 5 次后**退写成 `<OUT>_<时间戳>.csv`**。

后果：**最新数据落盘成功，但主表停留在旧版本**。历史实测因此低估过 **545 条**。

> 收尾合并时必须 `glob` 所有时间戳变体。`finalize_jd.py` 已同时纳入
> `forward 族` 和 `edge 族` 两条 glob —— 只 glob edge 是修完第一版后补上的。

### 1.2 分块产出要到块结束才并表

块跑到一半时主表一动不动，是**正常**的。判断是否真在产出要看 `out_<块号>_<pid>.json`
的 mtime 和条数，不要看主表。

### 1.3 正确口径

```
真实进度 = 全部 <DATASET>_含JD*.csv 并集 ∪ 两泳道在途 out_*.json 里"不在并集内"的 job_id
reverse 独有增量 = (edge 族 CSV ∪ edge 在途) − forward      ← 必须减，否则重复计数
```

用 `scripts/stat_progress.py` 直接算，别手搓。

### 1.4 `out_*.json` 的字段名是 `jd`

不是「岗位描述」，也不是 `job_description`。按错字段名统计会得到 0 条。

### 1.5 日志里「已有 JD N 条」不是全局进度

它是**该泳道源表 + 自身恢复池**，不含另一条泳道的在途产出。
全局真值得自己做 job_id 并集。

---

## 2. 并发与文件占用（本机最主要的崩溃来源）

### 2.1 凡是共享文件名，都要带 pid

同机并发不可控（safe-delete 策略、预览占用、杀不干净的旧实例）。
**同一类问题出现过三次**，每次都是文件名共享：

| 文件 | 修复 |
|---|---|
| `out_*.json` | `out_<块号>_<pid>.json` |
| `_jd_done_*.json` | `_jd_done_<方向>_<端口>.json` + tmp 也带 pid |
| `in_*.json` | `in_<块号>_<pid>.json` ← **第三次才补上** |

症状：

```
PermissionError: [WinError 5] 拒绝访问: 'out_00600_4480.json.tmp18504' -> 'out_00600_4480.json'
PermissionError: [Errno 13] Permission denied: '...\_JD补抓\in_00240.json'
```

后果：**整块只产出 1/120**，日志打 `本块产出偏低`。**这不是风控**，别去调速率。

### 2.2 实例锁必须用 TCP 端口

不能用锁文件 —— 本机 `os.remove()` 会失败，锁文件删不掉就永久残留。
端口由 OS 管理，进程退出自动释放。

Windows 上**必须**加 `SO_EXCLUSIVEADDRUSE`，否则默认允许重复绑定、锁形同虚设：

```python
s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
```

### 2.3 主循环必须兜底

```python
while True:
    try:
        main(); break
    except SystemExit:
        raise
    except Exception as exc:
        log(f"⚠️ 主循环异常，10 秒后自动继续：{type(exc).__name__}: {exc}")
        time.sleep(10)
```

绝不允许非关键路径的异常（写进度文件、写 TRACK）终止整个抓取。

### 2.4 「清理旧进程」会打断进行中的块

`start_backfill.py` 的 `kill_workers()` 会在块跑到一半时终止上一实例。

**好消息**：`absorb_existing()` 的恢复池机制**无数据丢失** —— 已抓的 JD 会被
下次启动收回，体现在「待补」数下降，只是主表条数暂时不涨。

**坏消息**：如果两个实例同时写同一个 `out_00000.json`，就是 2.1 那个 `PermissionError` 的成因。

> 块大小 120 + 块间 240s ≈ 10~60 分钟，与小时级定时任务同量级 ⇒ 每次触发都会打断。
> **更优配置：块大小降到 60，或把定时周期改成 2 小时。**

### 2.5 进程链不是重复实例

正常一个实例是 4 个 python 进程的父子链：

```
backfill_jd.py(持锁) → backfill_jd.py → boss_cdp_raw.py → worker
```

别看到一堆 python 就以为是重复实例去清理。**认实例看谁持有锁端口**。

---

## 3. 风控

### 3.1 三种状态要分清

| 状态 | 症状 | 处理 |
|---|---|---|
| 正常 | 产出稳定 | — |
| **软风控** | 页面跳 `/passport/` 的**安全验证**页，要求人机验证 | 人工滑块验证，**立刻恢复**，不用等 |
| **硬封** | 「访问受限」「将于 X 时刻恢复」 | 等解封，别重试 |

用 `diagnose/probe_block_scope.py` 做对照实验区分**IP 级**还是**账号级**：
匿名无 Cookie 正常 + 已登录被拦 ⇒ **账号级软风控**。

### 3.2 速率波动极大，别单看一块就下结论

实测同一夜里 **1.2s/条 ↔ 39s/条**，相差 10 倍以上。
单块低产先怀疑文件占用（见 2.1）和 CDP 抖动，**连续 2~3 块都低产**才考虑风控。

### 3.3 另一种"低产"：CDP 连接不稳

```
TimeoutError: CDP WebSocket recv 超时, method=Page.navigate
```

会导致连续几块只产出 5~15 条。与风控无关，重连即可。

### 3.4 低产 ≠ 报错

`本块产出偏低（1/120）` 后面往往跟一段**上一个错**的尾部日志。
先看尾部日志的异常类型，再判断原因。

### 3.5 启动器的 `restricted()` 门槛偏松

当前是 `hit >= max(2, len(pages)//2)`。实测 4 个标签页只有 1 个是 403 时不触发。
如果你发现反复无效重启，把它改成「≥1 个命中即告警」。

---

## 4. 环境与路径

### 4.1 缺 pandas

运行环境**没有 pandas**，统计必须用标准库 `csv` / `json`。写脚本时注意。

### 4.2 中文路径在 `wmic`/`cmdline` 里会乱码

`λ\_JDץ\in_00000.json` 其实是 `岗位数据\_JD补抓\in_00000.json`。
别照抄 cmdline 去定位文件。**用 `psutil` 拿命令行，用 Python 比对路径**。

### 4.3 模块级 import 顺序

`config.py` 必须能被 `sys.path` 找到。所有脚本顶部都有：

```python
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import ...
```

`scripts/diagnose/*.py` 要多退一级：`os.path.dirname(os.path.dirname(...))`。

### 4.4 openpyxl 不接受控制字符

写 Excel 前必须清洗 `[\000-\010]|[\013-\014]|[\016-\037]`（保留换行 `\n` 和制表 `\t`），
否则 `IllegalCharacterError`。`finalize_jd.py` 里有参考实现。

---

## 5. 孤儿文件

长时间运行会留下：

- `out_*.json.tmp<pid>` —— 重命名落盘失败留下的孤儿临时文件
- `_jd_done_*.json.<pid>` / `.tmp` —— 同上
- `claim_*.lock` —— 认领文件（**设计上永不删除**，靠里面的 pid 是否存活判断陈旧）

这些**不影响功能**，可以清理，但别在实例运行时清 `claim_*.lock`。

---

## 6. ⚠️ 危险操作：杀浏览器进程

`inject_cookies.py` 在拉起浏览器前要清掉旧的同 profile 进程。

**旧实现是"杀掉所有 `chrome.exe` / `msedge.exe`"** —— 那会把用户自己开着的
**个人浏览器（同样的进程名）一起杀掉，丢失未保存的标签页**。

现版本已改为**按 `--user-data-dir` 精确匹配**，只杀属于本流水线 profile 的进程：

```python
cl = " ".join(p.info.get("cmdline") or []).lower()
if target and target in cl:      # target = abspath(profile).lower()
    p.kill()
```

**改这段代码时的铁律**：匹配不到就**什么都不杀**，
绝不允许退化成"杀掉所有同名进程"。

同类需谨慎的还有：

- `cleanup_workers.py` / `start_backfill.py:kill_workers()` —— 按
  cmdline 含 `backfill_jd` / `boss_cdp_raw` 匹配，只杀本流水线的 python 进程，相对安全
- `inject_cookies.py` 里的 `psutil` 遍历 —— 任何 `p.kill()` 前都要先确认进程归属
