# 测试挂单（Test Place Orders）设计

日期：2026-05-19

## 目标

在仪表盘新增一个"测试挂单"按钮。用户点击后，用**第一个启用钱包**，按最近一次扫描得到的 eligible 市场（按竞争度升序）依次遍历，**直到成功挂出 3 个符合挂单策略的买单为止**，用于小批量验证后再走正式"分发挂单"。

## 背景与可见性说明

- **订单管理页 (`/api/orders`)**：挂单成功后立即可见——该页实时调 API `get_open_orders()` 读取钱包在 Polymarket 上真实挂着的单子。
- **历史记录页 (`/api/history`)**：单纯挂单**不会**写历史。`trades` 表只在监控线程检测到买单成交、挂出止盈卖单时写入。**测试挂单不要求先启动监控**：若第一个启用钱包恰好已有 running worker（监控在跑）则复用它，成交能被监控捕捉、自动挂止盈并写历史；否则临时构造 API 仅用于挂单——此时测试买单若成交，没有监控线程会自动挂止盈/写历史（用户已接受此权衡，测试只验证能否成功挂出符合策略的单，订单管理页可见即可）。

## 前置条件

点击"测试挂单"时：

1. 必须已「扫描」过：`manager.eligible_markets` 非空。否则返回中文提示"请先扫描市场"。
2. 必须至少有一个 `enabled` 钱包。否则返回中文提示"没有启用的钱包"。

**不要求**先启动监控/引擎。

## 行为定义

### "成功挂出一单"的定义

仅当 `api.place_limit_buy(...)` 调用成功（未抛异常）才计 1 单。以下情况**不计数、继续遍历下一个市场**：

- 该市场在冷却期（`db.is_in_cooldown`）
- 该资产已有挂单（`token_id in open_buy_assets`）
- 取 orderbook 失败 / 无 bids 或 asks
- `determine_order_price` 返回 `None`（不符合挂单策略）
- 余额不足
- `place_limit_buy` 抛异常

遍历完所有 eligible 市场仍不足 3 单，就有几单算几单（不报错）。

### 钱包选择

"第一个启用钱包" = 按 `db.list_wallets()` 返回顺序，第一个 `enabled` 的钱包（与是否启动监控无关）。没有任何 enabled 钱包 → 返回"没有启用的钱包"。

确定该钱包后取 worker：

- 若 `manager.engines[address]` 存在且 `running` → **复用**该 worker（成交可被监控捕捉、写历史）。
- 否则用该钱包密钥**临时构造** `PolymarketAPI` 与 `WalletWorker`（构造方式同 `start_wallet`：`decrypt` 私钥 → `PolymarketAPI(pk, funder=funder or None)` → `WalletWorker(api, db, address, settings)`），**不调用 `worker.start()`**（不起监控线程），仅用于本次挂单。

### 市场顺序

取**全部** `manager.eligible_markets`，按 `market_competitiveness` 升序排序（与现有 `place_all_orders` 同规则——竞争度最低优先，奖励份额更大）。不截断前 N 个。

## 组件设计

### 1. `WalletWorker.place_orders` 增加 `limit` 参数

签名改为 `place_orders(self, eligible_markets: list[dict], limit: int | None = None)`。

- `limit=None`（默认）：行为与现状**完全一致**——正式"分发挂单"路径不受影响。
- `limit` 为正整数：维护一个成功计数器，仅在 `place_limit_buy` 成功的那一支自增；计数达到 `limit` 后 `break` 退出市场遍历循环。
- 所有 `continue`（冷却 / 已挂 / orderbook 失败 / 价格 None / 余额不足 / 下单异常）分支均**不**自增计数。

其余逻辑（成交价重算、余额每单前重读、冷却跳过、跳过已挂资产、不写 DB）保持不变。

### 2. `EngineManager.test_place_orders()`（新方法）

```
def test_place_orders(self) -> dict:
    1. eligible_markets 为空 → return {"ok": False, "message": "请先扫描市场"}
    2. 按 market_competitiveness 升序排序全部 eligible_markets
    3. 取 db.list_wallets() 中第一个 enabled 钱包；没有 → return
       {"ok": False, "message": "没有启用的钱包"}
    4. 若该钱包 self.engines[address] 存在且 running → worker = 它；
       否则 try: 临时构造 worker（decrypt → PolymarketAPI → WalletWorker，
       不 start()）；构造异常 → return {"ok": False, "message": f"测试挂单失败：{e}"}
    5. try: worker.place_orders(sorted_markets, limit=3)
       except: log + return {"ok": False, "message": f"测试挂单失败：{e}"}
    6. return {"ok": True, "message": "已对符合策略的市场提交最多 3 个测试买单，请到订单管理查看"}
```

不改动 `place_all_orders`。复用 `engine/manager.py` 已导入的 `decrypt` / `PolymarketAPI` / `WalletWorker`。

### 3. 路由 `POST /api/engine/test-place-orders`（`web/routes.py`）

```
@app.route("/api/engine/test-place-orders", methods=["POST"])
@login_required
def api_test_place_orders():
    if not manager:
        return jsonify({"ok": False, "message": "引擎未启动"})
    return jsonify(manager.test_place_orders())
```

### 4. 前端（`web/templates/dashboard.html`）

- 在"分发挂单"按钮旁新增 `测试挂单` 按钮。
- 点击先 `confirm("将用第一个启用钱包的真实资金，最多挂出 3 个测试买单，确认？")`。
- 确认后 `POST /api/engine/test-place-orders`，把返回的 `message` 显示给用户（与现有按钮的提示方式一致）。

## 错误处理

- 所有前置条件不满足均返回 `{"ok": False, "message": <中文提示>}`，前端原样展示，不抛异常、不中断页面。
- `place_orders` 内单个市场的异常已被现有 `try/except` 各分支吞掉并记日志，不影响计数与遍历。

## 测试

- 新增单元测试：`place_orders(markets, limit=3)` 在第 3 单成功后停止，跳过的市场（冷却/价格 None 等）不计数继续遍历。
- 新增单元测试：`place_orders(markets)`（无 limit）行为不变（回归保护）。
- 新增单元测试：`test_place_orders()` 在 `eligible_markets` 为空、无 running worker、正常三种情况下的返回值。
- 现有 `tests/test_manager.py` 相关用例保持通过（默认参数不变）。

## 不做（YAGNI）

- 不为测试挂单单独做逐单明细回执——用户通过订单管理页查看实况。
- 不改动已测试过的 `place_orders` 核心挂单逻辑（仅加 `limit` 早停）。
- 不新增配置项（3 为固定值）。
