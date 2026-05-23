# 设计：全局黑名单（按 condition_id 拉黑，挂单拦截 + 撤已挂买单 + 管理界面）

日期：2026-05-23

> 这是用户「订单管理改进」第二个子项目。第一个（展示优化 + Gamma 兜底补名）已完成，spec/plan 在 `docs/superpowers/`。

## 背景与目标

用户希望能把某个市场拉黑，让做市引擎不再碰它。需求：

1. 按 **condition_id** 拉黑（一个市场）。
2. 黑名单**全局公用**（不分钱包）。
3. 引擎运行期，**任何钱包都不得再挂**该 condition_id 的 YES 或 NO 买单。
4. 当前挂单页每行在「撤单」旁加一个「加入黑名单」按钮。
5. 前端新增一个黑名单管理编辑界面。

经 brainstorm 确认的关键行为：

- **加入黑名单时**：撤掉**所有钱包**挂在该 condition_id 上的**买单**（YES/NO 都撤）+ 拦截未来挂单；**已成交持仓不动**（持仓由止盈/止损管，黑名单只管买单），止盈卖单也不动。
- **管理界面**：独立导航页「黑名单」，含「粘贴 condition_id + 备注 → 加入」、列表（市场名+链接、condition_id+复制、加入时间、移除）。

### 相关现状（决定方案的约束）

- DB 无黑名单表；`cooldowns` 是按 `(wallet, market_id)` 的范式可借鉴（`models/database.py`）。
- `engine/manager.py` `place_orders` 是**所有钱包**（自动/手动两条路径）挂单的唯一入口，循环开头已有 `if self.db.is_in_cooldown(self.wallet_address, market["market_id"]): continue`（约 `manager.py:142`）——黑名单拦截放这里最自然。
- `engine/scanner.py` 预筛里已有 cooldown 检查（约 `scanner.py:95`）；scanner 是单一共享线程，产出全局 eligible 列表。
- 订单的 `market` 字段就是 condition_id（CLOB `get_open_orders` 返回；`/api/orders` 已透出为 `market`）。一个 condition_id 含 YES/NO 两个 token，**按 condition_id 拦截天然覆盖两个方向**。
- `web/routes.py` 有 `_wallet_apis()`（拿到各钱包 API）、`cancel_orders`、以及 `_enrich_rows(rows, id_key)`（补市场名+链接，已支持 Gamma 兜底）。
- 前端 `marketCell(name, conditionId, url)`（`app.js`）+ `.table-scroll` 等已就绪，可直接复用。

## 方案

### 第 1 节 · 数据层

新建表（`models/database.py` `_create_tables`，新表用 `CREATE TABLE IF NOT EXISTS` 即可，老库自动建，无需迁移分支）：

```sql
CREATE TABLE IF NOT EXISTS blacklist (
    condition_id TEXT PRIMARY KEY,
    note TEXT NOT NULL DEFAULT '',
    added_at REAL NOT NULL DEFAULT (strftime('%s','now'))
);
```

**全局表，无 wallet 列。** 新增方法：

- `add_to_blacklist(condition_id, note="")`：`INSERT OR REPLACE`（空 condition_id 跳过；`added_at` 用 `time.time()`）。
- `remove_from_blacklist(condition_id)`：`DELETE`。
- `get_blacklist() -> list[dict]`：`SELECT *`，按 `added_at DESC`，供管理界面（含 note/added_at）。
- `get_blacklist_ids() -> set[str]`：`SELECT condition_id`，返回 set，供拦截路径一次性加载。

### 第 2 节 · 拦截（核心行为）

双保险，两处都按 condition_id（= `market_id`）判断：

- **`engine/manager.py` `place_orders`**（权威闸门）：在 `for market in eligible_markets:` 循环**之前**加载一次 `blacklist = self.db.get_blacklist_ids()`，循环里在 cooldown 检查旁加：
  ```python
  if market["market_id"] in blacklist:
      continue
  ```
  任何钱包都走这个 place_orders，所以全局生效；YES/NO 两 token 同属一个 market_id，一并拦下。
- **`engine/scanner.py`**（保持列表干净）：`scan()` 开头一次性 `blacklist = self.db.get_blacklist_ids()`，预筛里在 cooldown 检查旁加 `if condition_id in blacklist: continue`。这样仪表盘不再显示它、也不会被分发。（与 place_orders 用同一个 `get_blacklist_ids()` set，不引入额外的 `is_blacklisted` 方法。）

> 为什么两处都做：scanner 让它进不了列表是常态防线；place_orders 落单前再挡一道，覆盖「名单中途新增」「内存 eligible 列表过期」「手动挂单路径」。

### 第 3 节 · 加入黑名单 = 同时撤掉已挂买单

`POST /api/blacklist {condition_id, note}` 做两件事：

1. `db.add_to_blacklist(condition_id, note)`；
2. **遍历所有钱包**（`_wallet_apis()`），撤掉它们挂在该 condition_id 上的**买单**（side==BUY，order.market==condition_id；YES/NO 都撤）。**止盈卖单（SELL）和已成交持仓不动。**

撤单过滤逻辑抽成纯函数便于单测，放 `engine/blacklist_ops.py`（新模块，零外部依赖）：

```python
def buy_order_ids_for_condition(orders: list, condition_id: str) -> list:
    """从一个钱包的 open orders 里挑出该 condition_id 的 BUY 单 id。"""
    return [
        o["id"] for o in orders
        if o.get("side") == "BUY" and o.get("market") == condition_id and o.get("id")
    ]
```

路由里：对每个 wallet api，`ids = buy_order_ids_for_condition(api.get_open_orders(), condition_id)`，非空则 `api.cancel_orders(ids)`，单钱包失败记日志不中断其它钱包。登录后即可用，不依赖引擎是否运行（撤单走 `_wallet_apis()`；未来拦截在引擎运行时由 place_orders/scanner 生效）。

### 第 4 节 · API

- `GET /api/blacklist`：`db.get_blacklist()` → 用 `_enrich_rows(rows, "condition_id")` 补 `market_name`+`market_url`（复用展示优化的机制，含 Gamma 兜底），返回。
- `POST /api/blacklist {condition_id, note?}`：基本校验（`condition_id` 非空）→ 加入 + 撤已挂买单（第 3 节）→ 返回 `{ok, cancelled: N}`。
- `DELETE /api/blacklist/<condition_id>`：`db.remove_from_blacklist` → `{ok}`。

### 第 5 节 · 前端

- 新建 `web/templates/blacklist.html`，`web/templates/base.html` 导航加「黑名单」入口（在「监控状态」与「退出」之间），`web/routes.py` 加 `/blacklist` 页面路由 + `blacklist_page()`。页面：
  - 顶部添加区：`<input>` 粘贴完整 condition_id + `<input>` 备注 + 「加入」按钮 → `POST /api/blacklist`，成功后清空输入并刷新列表（若 `cancelled>0` 提示「已撤掉 N 笔买单」）。
  - 表格：列「市场」（`marketCell(r.market_name, r.condition_id, r.market_url)`）、condition_id（已在 marketCell 的 📋 里，可不单列）、加入时间、备注、移除按钮（`DELETE` 后刷新）。
- `web/templates/orders.html` 当前挂单行「撤单」旁加「加入黑名单」按钮：`confirm('加入黑名单将撤掉所有钱包在该市场的买单，确定？')` → `POST /api/blacklist {condition_id: o.market}` → 刷新订单。

### 第 6 节 · 错误处理与边界

- 加入黑名单时某钱包撤单失败：记日志，继续其它钱包，不让整个请求失败。
- 重复加入同一 condition_id：`INSERT OR REPLACE` 幂等（更新 note/added_at）。
- 粘贴的 condition_id 仅做非空校验（不强校验 0x 格式，避免误拒）。
- 拦截路径加载 `get_blacklist_ids()` 失败：不应让 place_orders/scanner 崩；按空集处理（最坏是没拦住，下次再说），但正常 SQLite 本地读不会失败。
- 移除黑名单不自动恢复任何东西（只是以后能再挂）。

## 测试

- `tests/test_database.py`：`add_to_blacklist`/`remove_from_blacklist`/`get_blacklist`/`get_blacklist_ids`（加入可取回、去重、移除、空集、空 condition_id 跳过）。
- `tests/test_place_orders.py`：黑名单市场被 `place_orders` 跳过（不 `place_limit_buy`），非黑名单照常；用 mock db 的 `get_blacklist_ids` 返回集合。
- `tests/test_scanner.py`：黑名单 condition_id 不进 eligible 列表。
- 新建 `tests/test_blacklist_ops.py`：`buy_order_ids_for_condition` 只挑匹配 condition_id 的 BUY（SELL 不挑、别的市场不挑、缺 id 不挑）。
- 路由级（项目无基建）：靠上述纯函数 + DB 单测覆盖核心，手动验证页面。

## 不在本子项目范围

- 已成交持仓的平仓（黑名单只管买单）。
- 黑名单的导入/导出、批量操作、过期自动移除（YAGNI）。
