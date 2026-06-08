# Polymarket 做市奖励工具 · Polymarket Market-Making Rewards Bot

> 本地单用户的 Polymarket 自动做市工具，通过挂单赚取流动性奖励（liquidity rewards）。
> A local, single-user app that automates reward-farming market making on Polymarket.

![version](https://img.shields.io/badge/version-1.0.15-blue)
![python](https://img.shields.io/badge/python-3.10%2B-blue)
![flask](https://img.shields.io/badge/flask-3.1-black)
![platform](https://img.shields.io/badge/platform-Windows-lightgrey)

中文说明在前，English notes follow each section.

---

## ⚠️ 风险免责声明 / Disclaimer

**中文**

- 本工具会使用你的**真实钱包私钥**在 Polymarket 上**下真实订单、动用真实资金**。
- 做市与持仓本身存在亏损风险（行情、滑点、未成交、平台规则变化等）。本工具不保证盈利，也不构成任何投资建议。
- 你需自行承担使用本工具产生的一切后果，包括但不限于资金损失。请先用小额资金充分测试后再决定是否加大投入。
- 请遵守 Polymarket 的服务条款及你所在司法辖区的法律法规。

**English**

This software places **real orders with real funds** on Polymarket using **your real wallet private key**. Market making carries financial risk and is **not** investment advice. Use at your own risk, test with small amounts first, and comply with Polymarket's Terms of Service and your local laws.

---

## 这是什么 / What is this

面向**非技术用户**设计：下载安装包 → 双击运行 → 浏览器自动打开 → 设置密码并录入钱包私钥 → 一键启动，程序自动扫描符合奖励条件的市场、挂单、监控成交并执行止盈/止损。

Built for **non-technical users**: install, double-click, a browser opens, you set a password and enter your wallet private key, then the app scans reward-eligible markets, places orders, and monitors fills with automatic take-profit / stop-loss.

技术栈 / Stack：Python + Flask（本地 Web 应用，仅监听 `127.0.0.1`），通过 [`py-clob-client-v2`](https://pypi.org/project/py-clob-client-v2/) 访问 Polymarket CLOB；打包为单个 Windows 安装程序（PyInstaller + Inno Setup）。

---

## 功能特性 / Features

- **自动 / 手动两种模式**
  - *自动*：恢复 → 扫描 → 下单 → 监控的全流程循环。
  - *手动*：分步执行「扫描 / 下单 / 启动监控」，由你掌控每一步。
- **奖励做市选品**：按最低奖励、结算天数、价格区间、买卖价差、冷却时间过滤市场，并按竞争度（competitiveness）从低到高优先下单（竞争越少奖励份额越大）。
- **持仓驱动的止盈**：成本价由我们真实的 CLOB 成交逐笔重建（FIFO 净额），每个持仓**始终只挂一张**卖单；卖价 = `max(向上取整到 tick 的成本, 最优买价 + 1 tick)`（穿价护栏：不亏本卖、永远做 maker、不吃单）。成本无法可靠重建时**跳过并显著告警**（绝不按不确定成本卖出，自愈式重试）。
- **止损**：按持仓成本与现价的偏离百分比触发。
- **全局黑名单**：在下单 / 扫描 / 监控三处统一拦截不想参与的市场。
- **私钥本地加密**：钱包私钥用 AES-256-GCM 加密，密钥由你的密码经 PBKDF2（60 万次迭代）派生，仅存在于内存。
- **GitHub 自动更新**：启动时与最新 Release 比对，弹窗 → 下载校验（SHA-256）→ 静默安装重启。

> Two modes (auto/manual), reward-aware market selection, position-driven take-profit with cost reconstructed from real fills, stop-loss, a global blacklist enforced at three choke points, AES-256-GCM encrypted keys held only in memory, and GitHub Release auto-update.

---

## 快速开始（普通用户）/ Quick Start (end users)

1. 打开本仓库的 [**Releases**](../../releases) 页面，下载最新的 `PolymarketMarketMaker_Setup.exe`。
2. 运行安装程序完成安装（运行时数据会写入 `%LOCALAPPDATA%\PolymarketMarketMaker`，不会污染安装目录）。
3. 从开始菜单/桌面双击启动；浏览器会自动打开 `http://127.0.0.1:8765`。
4. **首次运行**进入 `/setup`：设置一个登录密码（用于加密你的私钥，**忘记不可找回**）。
5. 录入钱包私钥（仅需私钥，程序会自动推导你的 Polymarket 资金存款地址 / Gnosis Safe）。
6. 确认参数后启动引擎。**建议先用小额资金试跑**，观察止盈/止损与挂单是否符合预期。

> Download the latest installer from **Releases**, install, run, open `http://127.0.0.1:8765`, set a login password (used to encrypt your key — **not recoverable if lost**), enter your wallet private key, and start the engine. Test with small amounts first.

⚠️ **停止引擎会同时停止止损监控**：已停止时仍持有的仓位将不再被止损保护——界面对此有明确提示，请留意。

> Stopping the engine also stops stop-loss monitoring; open positions are then unprotected.

---

## 从源码运行（开发者）/ Run from Source

环境要求：Python 3.10+（Windows 为主要目标平台）。

```powershell
pip install -r requirements.txt   # 运行依赖
pip install pytest                # 跑测试用

python app.py                     # 启动；浏览器打开 http://127.0.0.1:8765（首次为 /setup）

pytest                            # 全部单元测试（纯逻辑，不联网）
pytest tests/test_strategy.py     # 单个文件
```

依赖 / Dependencies：`flask`、`py-clob-client-v2`、`py-builder-relayer-client`、`cryptography`（见 `requirements.txt`）。

> `tests/` 是纯逻辑、不联网的 pytest 单测。仓库根目录的 `test_live.py`、`test_simulate.py`、`test_real_order.py` **不是** pytest 测试，而是会命中真实 Polymarket API / 本地数据库的手动脚本（`test_real_order.py` 会下真实订单），**请勿**纳入测试套件运行。

> The files `test_live.py` / `test_simulate.py` / `test_real_order.py` at the repo root are manual scripts that hit the **live** API (the last one places **real** orders) — they are not part of the pytest suite.

---

## 工作原理 / Architecture

**鉴权门控一切。** 钱包私钥用从密码派生的密钥加密，密钥只在登录后存在于内存——因此引擎**无法开机自启**，必须先登录。`EngineManager` 在 `/setup` 与 `/login` 路由中、拿到加密密钥后才构造。整体为**单进程、单用户**设计。

**线程模型（`engine/manager.py`）。** 一个共享的 `MarketScanner` 线程产出「合格市场列表」（扫描无需特定钱包鉴权）；每个启用的钱包对应一个 `WalletWorker` 线程，负责（a）从共享列表下单、（b）监控成交/止损。SQLite 连接以 `check_same_thread=False` 打开并在线程间共享。

**流水线：扫描 → 策略 → 下单 → 监控 / scan → strategy → place → monitor。**

| 阶段 | 模块 | 职责 |
| --- | --- | --- |
| 扫描 Scan | `engine/scanner.py` | 过滤奖励市场（奖励阈值、结算天数、价格带、价差、冷却），记录每市场的 `min_cost`（达标最低资金），下单时各钱包据此把关。 |
| 策略 Strategy | `engine/strategy.py` | `determine_order_price` 纯函数、完整单测、核心 IP：依据 tick size 与 `rewards_max_spread`，从订单簿买盘深度选买价。 |
| 下单 Place | `engine/manager.py` | 下单前**每次重读余额**防并发超支；按竞争度从低到高下单。 |
| 监控 Monitor | `engine/monitor.py` | 检测成交、维护每仓**唯一**卖单（止盈，成本逐笔重建）、止损、重核价差与价格区间。 |

> Auth gates everything (engines can't auto-start). One shared scanner thread feeds per-wallet workers; SQLite is shared across threads. The pipeline is scan → strategy → place → monitor, with `engine/strategy.py` (pure, fully unit-tested) as the core IP.

更详细的设计文档（简体中文）见 `docs/superpowers/specs/2026-05-17-polymarket-market-maker-design.md`。
开发约定与关键不变量见 [`CLAUDE.md`](CLAUDE.md)。

---

## 核心参数 / Configuration

默认值见 `config.py` 的 `DEFAULTS`；用户在界面上的修改按 key 存入数据库 `settings` 表，并在读取时合并覆盖。改动在下次启动引擎时生效。

| Key | 默认值 | 含义 |
| --- | --- | --- |
| `min_reward_usd` | 100.0 | 市场最低奖励门槛（美元） |
| `max_spread_cents` | 3.0 | 最大买卖价差（美分） |
| `min_price_cents` / `max_price_cents` | 10.0 / 50.0 | 单价区间（美分，含端点） |
| `min_settlement_days` | 4 | 距结算的最少天数 |
| `stop_loss_pct` | 15.0 | 止损触发偏离百分比 |
| `scan_interval_sec` | 30 | 自动模式扫描间隔（秒） |
| `fill_check_interval_sec` | 5 | 成交/监控检查间隔（秒） |
| `cooldown_minutes` | 20 | 同市场成交后的冷却（分钟） |
| `max_buy_orders_per_wallet` | 5 | 每钱包最大同时挂买单数 |
| `order_size_mode` | `min` | 下单规模模式（`min` / 自定义） |
| `order_size_custom_usd` | 0.0 | 自定义下单金额（美元） |

运行时数据位置 / Runtime data：
- 打包运行：`%LOCALAPPDATA%\PolymarketMarketMaker\`（`market_maker.db`、`market_maker.log`）
- 源码运行：项目根目录
- 监听地址：`http://127.0.0.1:8765`（端口被占用时自动回退到空闲端口）

---

## 打包与发版 / Build & Release

```powershell
# 构建 Windows 安装包(PyInstaller 打 exe + Inno Setup 打安装程序)
powershell -ExecutionPolicy Bypass -File build_installer.ps1

# 一键发版:构建 -> 计算 SHA-256 -> 打 git tag -> gh release create
# 发版前先修改 version.py 的版本号
powershell -ExecutionPolicy Bypass -File release.ps1
```

- 版本号唯一来源：`version.py`（被 build / release / 自动更新共同读取）。
- 发版需要已安装并登录的 [GitHub CLI](https://cli.github.com/)（`gh auth login`）。
- 根目录若存在 `RELEASE_NOTES.md` 则作为 Release 说明，否则自动生成。

> Single source of version truth is `version.py`. `release.ps1` builds the installer, computes SHA-256, tags, and creates a GitHub Release via the `gh` CLI.

---

## 安全说明 / Security

- 私钥使用 **AES-256-GCM** 加密；加密密钥由你的登录密码经 **PBKDF2（600,000 次迭代）** 派生，**仅存在于内存**，不落盘明文。
- 应用仅监听本机 `127.0.0.1`，不对外开放。
- 单进程、单用户设计：`web/routes.py` 持有模块级的 `db` / `manager` / `encryption_key`。
- 你只需输入私钥；资金存款地址（Polymarket Gnosis Safe）由签名 EOA 自动推导（CREATE2）。

> Keys are AES-256-GCM encrypted with a PBKDF2 (600k iterations) key held only in memory; the app binds to `127.0.0.1` only.

---

## License

本仓库**当前未声明开源许可证**（All rights reserved）。如需复用/二次分发，请先与作者确认。

This repository currently has **no open-source license** (all rights reserved). Contact the author before reuse or redistribution.
