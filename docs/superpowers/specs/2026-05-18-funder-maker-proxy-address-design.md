# 设计：修正下单 maker 地址（funder = Gnosis Safe 代理地址）

日期：2026-05-18
状态：已批准（待用户复核 spec）

## 背景与问题

对照 Polymarket CLOB 下单 API 文档（`POST /order`），订单对象中：

- `signer` = 私钥对应的 EOA 地址（签名者）
- `maker` = 持有资金的代理钱包地址（对 `signature_type=2` / Gnosis Safe 而言，即 Polymarket 代理钱包）

当前 `api/polymarket_api.py:52` 的兜底逻辑：

```python
funder=funder or temp_client.get_address(),
```

由于所有调用点（`web/routes.py:240`、`engine/manager.py:275/346/406`）都以
`PolymarketAPI(private_key)` 形式构造、从不传 `funder`，而默认
`signature_type=2`（Gnosis Safe），导致订单的 `maker` 被静默兜底成
**EOA 地址**，而非持有资金的 Gnosis Safe 代理地址。后果：maker 与实际资金账户
不一致，下单会因余额/授权不匹配而失败或匹配不到。

数据库 `wallets.funder` 列虽存在且有值（`0x98d6...ae75`），但：

1. 该值是早期 deposit-wallet（`signature_type=3` / POLY_1271）实验写入的
   （git `4e12601`/`f6e43e9`），最新 `1139272` 已 revert 回 sig=2 并说明
   "资金在 proxy wallet，不在 deposit wallet"——该值很可能是 deposit wallet
   地址，与 sig=2 需要的 Safe 地址不一致；
2. 当前 `add_wallet`/`list_wallets` 既不写也不读该列，数据流断裂。

## 目标

让 sig≠0 时下单的 `maker` 永远等于由私钥确定性派生出的 Polymarket
Gnosis Safe 代理地址，且改动面最小、不依赖来源可疑的 DB 旧值、无需数据迁移即可
保证运行时正确。

## 选定方案：方案 A —— 在构造函数内派生

否决的替代方案：

- **方案 B（添加时存库、各处从 DB 读）**：改动 5 个文件、需迁移已存钱包的旧
  funder、任何漏改的调用点会静默退回 bug。
- **方案 C（缺 funder 即抛错强制传入）**：最安全但改动最大，把"算地址"责任推给
  每个调用点，对非技术用户不友好。

方案 A 精准修掉那行兜底 bug，现有 4 处调用点无需改动即自动正确，无法被"漏改某
调用点"破坏，并彻底绕开 DB 中来源不明的旧值。

## 技术依据

- `RelayClient.__init__`（`py_builder_relayer_client/client.py:37`）纯离线：仅存
  url 字符串、取合约配置、由私钥构造 `Signer`，无网络调用。
- `RelayClient.get_expected_safe()`（同文件 `:225`）= `derive(EOA地址,
  safe_factory)`，纯 CREATE2 计算，无网络、无需 builder 凭证。
- 这正是 Polymarket 官方工具（本仓库 `diagnose_deposit_wallet.py`）使用的同一套
  派生逻辑；该脚本注释中称其为 "deposit wallet" 系命名混淆，其本质是 Safe 代理
  地址。
- `py-builder-relayer-client>=0.0.1` 已在 `requirements.txt` 声明。

## 详细设计

### 1. `api/polymarket_api.py`（核心改动）

1. 新增模块常量：
   ```python
   RELAYER_URL = os.environ.get("RELAYER_URL", "https://relayer-v2.polymarket.com/")
   ```
   （与 `diagnose_deposit_wallet.py` 一致，可被环境变量覆盖；`CHAIN_ID=137` 已存在）

2. 新增静态方法 `derive_proxy_address(private_key: str) -> str`：
   ```python
   relayer = RelayClient(RELAYER_URL, CHAIN_ID, private_key, None)
   return relayer.get_expected_safe()
   ```
   纯本地派生，无网络。

3. 改写 `__init__` 的 funder 解析（替换 `funder=funder or temp_client.get_address()`）：
   - 显式传入 `funder` → 用之；
   - `signature_type == 0`（EOA）→ 用 `temp_client.get_address()`
     （maker == signer == EOA，保持现状，EOA 下本就正确）；
   - 其它（sig=1/2/3）→ `funder = derive_proxy_address(private_key)`。

4. 最终 funder 存为 `self.funder`，供展示/日志/测试读取。

5. 派生失败（导入失败或异常）→ 抛出明确异常，简体中文消息
   （如 `无法派生代理钱包地址: {e}`），**绝不静默退回 EOA**。

效果：现有 4 处 `PolymarketAPI(private_key)` 无需改动即自动使用正确的 Safe
maker 地址。

### 2. `models/database.py`

- `add_wallet(self, address, encrypted_key, funder=None)`：INSERT 带上 `funder`
  列（列已存在）。
- `list_wallets()`：SELECT 增加 `funder`，供仪表盘展示。
- 新增 `update_wallet_funder(self, address, funder)`：供回填使用。

### 3. `web/routes.py` · `api_add_wallet`

- 构造 `api = PolymarketAPI(private_key)` 后，`api.funder` 即正确 Safe 地址。
- 持久化改为 `db.add_wallet(address, encrypted, api.funder)`。
- 返回体改为 `{"ok": True, "address": address, "funder": api.funder}`。
  `web/templates/config.html:120` 已 `alert(data.funder)`，前端无需改动。

### 4. 已存钱包数据回填 · `engine/manager.py` · `startup_recovery`

`startup_recovery` 本就遍历钱包并解密私钥。对每个钱包重算
`PolymarketAPI.derive_proxy_address`，若与 DB `funder` 不一致则调用
`db.update_wallet_funder` 更新。零新增入口，范围可控。
（注：运行时下单正确性不依赖此回填——构造函数已重新派生；回填仅为修正 UI 显示
与清除来源不明的旧值。）

### 5. 错误处理

- `api_add_wallet` 已有 `except Exception as e` 包裹构造，派生失败会以
  "私钥无效 / 无法派生代理钱包地址" 回显给用户。
- EOA（sig=0）路径不受影响。

### 6. 测试

- 新增 `tests/test_proxy_derivation.py`（纯逻辑、无网络，符合项目 `tests/`
  约定）：用固定测试私钥断言 `PolymarketAPI.derive_proxy_address`：
  - 输出确定（同输入同输出）；
  - 为合法 0x checksum 以太坊地址；
  - 与该私钥的 EOA 地址不同。
- 构造函数整体（含联网的 `derive_api_key()`）不纳入 pytest，由
  `test_simulate.py` 手动验证：`api.funder` 与 polymarket.com 显示一致，且
  `get_balance()` 返回预期非零余额（证明 maker 指向有资金的账户）。

## 范围外（不做）

- Safe 链上部署（`diagnose_deposit_wallet.py` 已覆盖；用户已在 Polymarket 交易
  即说明 Safe 已部署）。
- `signature_type=3`（POLY_1271 / deposit wallet）路径，已废弃。
- 不改默认 `signature_type`（保持 2）。

## 受影响文件汇总

| 文件 | 改动 |
|---|---|
| `api/polymarket_api.py` | 新增 RELAYER_URL 常量、`derive_proxy_address` 静态方法、改写 `__init__` funder 解析、`self.funder` |
| `models/database.py` | `add_wallet` 带 funder、`list_wallets` 返回 funder、新增 `update_wallet_funder` |
| `web/routes.py` | `api_add_wallet` 持久化并返回 `api.funder` |
| `engine/manager.py` | `startup_recovery` 回填 funder |
| `tests/test_proxy_derivation.py` | 新增纯逻辑测试 |

## 验收标准

1. `PolymarketAPI(pk)`（sig=2，未传 funder）后 `api.funder` 为派生出的 Safe
   地址，且 ≠ `api.get_address()`（EOA）。
2. `PolymarketAPI(pk, signature_type=0)` 的 funder 仍为 EOA。
3. `PolymarketAPI(pk, funder=X)` 的 funder 为显式传入的 X。
4. 添加钱包后 DB `wallets.funder` 与返回 JSON `funder` 均为派生 Safe 地址；
   前端弹窗显示该地址。
5. 程序启动后，已存钱包的 DB `funder` 被回填为派生 Safe 地址。
6. `pytest tests/test_proxy_derivation.py` 通过。
7. 手动 `test_simulate.py`：`api.funder` 与 polymarket.com 一致，
   `get_balance()` 非零。
