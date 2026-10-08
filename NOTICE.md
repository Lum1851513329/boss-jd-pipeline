# 第三方组件与归属声明

本项目在 `scraper/` 目录下**内置**了一个上游开源项目的源码副本，用于完成
BOSS 直聘的页面数据抓取（列表与详情页）。按 MIT 许可证要求，在此保留其版权声明。

---

## boss-zhipin-scraper

| 项 | 内容 |
|---|---|
| 项目 | `boss-zhipin-scraper` |
| 作者 | eatmoreduck |
| 版本 | v2.2.0 |
| 许可证 | MIT |
| 仓库 | https://github.com/eatmoreduck/boss-zhipin-scraper |

**内置的文件：**

- `scraper/boss_cdp_raw.py` — 抓取主脚本（通过 Chrome DevTools Protocol 接管已登录浏览器，
  旁听页面自身的 XHR 请求，从而拿到明文薪资与完整 JD）
- `scraper/job_summary.py` — 抓取结果摘要 / 求职提示词生成
- `scraper/requirements.txt` — 上游声明的依赖
- `scraper/LICENSE.upstream-MIT` — 上游 MIT 许可证原文

**本项目的改动：** 无。上述文件为**原样副本**，未做任何修改，
以保证上游更新时可以干净地替换。本项目只通过 `subprocess` 调用它，
并依赖其命令行接口（`--input` / `--detail` / `--max-details` / `--cdp-port` / `--detail-output`）。

> ⚠️ 如果你想 fork 并修改 `scraper/` 下的代码，请注意该目录仍受 MIT 许可证约束，
> 与项目根目录的 LICENSE 相互独立。

---

## 运行环境依赖

以下为通过 `pip` 安装的第三方库，**不包含**在本仓库中：

| 库 | 用途 | 许可证 |
|---|---|---|
| [patchright](https://pypi.org/project/patchright/) | 浏览器自动化 / CDP 连接 / Cookie 注入 | Apache-2.0 |
| [psutil](https://pypi.org/project/psutil/) | 进程枚举、端口属主查询（实现实例锁） | BSD-3-Clause |
| [websocket-client](https://pypi.org/project/websocket-client/) | CDP WebSocket 通信 | Apache-2.0 |
| [openpyxl](https://pypi.org/project/openpyxl/) | 导出 XLSX | MIT |
| [requests](https://pypi.org/project/requests/) | HTTP 请求 | Apache-2.0 |

## 可选依赖

`scripts/collect_city.py` 与 `scripts/collect2.py` 额外依赖 **`boss-agent` CLI**
（PyPI 包 `boss_agent_cli`，提供 `boss.exe`）。这不是本项目的作品，本项目仅调用其命令行接口。
日常补抓流程**不需要**这两个脚本。

---

## 数据来源与免责声明

本项目抓取的是 **BOSS 直聘（zhipin.com）** 上**公开可见**的招聘信息。
BOSS 直聘及其相关商标归其权利人所有，本项目与 BOSS 直聘无任何隶属或合作关系。

使用者应自行确保其使用行为符合：

- BOSS 直聘的用户协议与 robots 约定
- 目标司法辖区的数据保护与个人信息相关法律（如《个人信息保护法》）
- 不将抓取数据用于批量转售、骚扰招聘方或侵犯他人隐私

**请控制抓取频率，仅用于个人学习与研究。**
