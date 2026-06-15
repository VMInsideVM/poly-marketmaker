# SP1：模板与配置解耦 + 采集器拆分（设计 / spec）

> 日期：2026-06-15
> 状态：待用户评审
> 这是 v4 做市策略接入的第一个子项目（地基）。父背景见本文「零、背景与定位」。

## 零、背景与定位

要把 `Polymarket做市策略-v2.md`（实为「定稿 v4」）接入当前做市系统。已确定的总方针：

- **老的「单边单单」策略退役，v4 完全替代**（不做开关、不双跑、不融合）。
- **原地分层替换**：保留鉴权 / 加密 / 钱包导入 / funder 派生 / `PolymarketAPI` 封装 / 成本重构（get_trades 加权 + 裸奔跳过）/ DB 层 / Flask 外壳 / 自动更新等基础设施，只整体替换「决策层」（策略算法、配置模型、离场逻辑）。
- v4 拆成可独立 spec→plan→实现的子项目，依赖顺序：

  ```
  SP1 模板与配置解耦（本文）  ← 地基
       ├─ SP2 多档挂单（重写 strategy.py）
       ├─ SP3 三段式离场（重写 monitor 离场）
       ├─ SP4 单份奖励阈值 + 取档（品类排除已在 SP1 落地）
       ├─ SP5 三档节奏 + 观察名单（重构 manager 循环）
       └─ SP6 模板管理 UI
  ```

**SP1 的边界（关键）**：只做「配置从哪来」和「采集器怎么拆」这两件管道工程，**不碰策略算法**——每钱包的「定价」步骤仍调用现有的老 `determine_order_price`。这样 SP1 完成后系统能端到端跑、可验证「行为不变（除品类排除）但配置已走模板」，再进 SP2 换算法。这是一个可独立验证的 checkpoint，把回归风险压到最小。

## 一、已确认的两个架构事实

1. **多钱包会并行绑定不同模板**（筛选门槛不一样）。所以「一个共享 scanner + 一个全局 eligible 列表」的现有架构不再成立，必须拆成「共享候选采集 + 每钱包按自己模板筛选」。
2. **品类过滤在采集阶段完成，用黑名单（排除指定品类）**。经真实接口对照实验确认（2026-06-15）：
   - CLOB `GET /rewards/markets/multi` **支持 `tag_slug` 服务端过滤**，且**本身就带每日奖励率 `rate_per_day`**。不存在的 tag 返回 0 条；`sports` 与 `politics` 结果零重叠；重复传 `tag_slug` 即 OR 并集。
   - 因此**不需要 join Gamma 补品类**（Gamma 仍只用于市场名 / slug 的现有用途）。
   - 品类 slug 实测：**体育 = `sports`、电竞 = `esports`、天气 = `weather`**。`esports` 是独立 tag——`esports ⊄ sports`、两者仅重叠约 47/100，所以**只排 `sports` 会漏掉一半电竞，必须三个一起排**。默认排除集 = `["sports", "esports", "weather"]`。

## 二、配置作用域：引擎级 vs 模板级

系统有两层配置，分表存储、各有去向：

| 作用域 | 参数 | 存储 | 前端 |
| --- | --- | --- | --- |
| **引擎级（全局单值）** | `scan_interval_sec`、`fill_check_interval_sec`、`cooldown_minutes`、`rewards_cache_ttl_sec` | 保留现有 `settings` 表 | 「全局参数」区 |
| **策略级（每钱包 / 每模板）** | `min_reward_usd`、`max_spread_cents`、`min_price_cents`、`max_price_cents`、`min_settlement_days`、`stop_loss_pct`、`max_buy_orders_per_wallet`、`order_size_mode`、`order_size_custom_usd`、**新增 `excluded_categories`**，以及未来 SP2–SP5 的全部 v4 参数 | 模板（`template_settings` 表） | 模板编辑（SP6） |

`config.py` 的 `DEFAULTS` 据此拆成 `ENGINE_DEFAULTS` 与 `TEMPLATE_DEFAULTS` 两个字典。

> 现有 `DEFAULTS` 中 `scan_interval_sec=30`、`fill_check_interval_sec=5`、`cooldown_minutes=20`、`rewards_cache_ttl_sec=600` 归引擎级；其余 9 个键归策略级。新增策略级键 `excluded_categories` 默认 `["sports","esports","weather"]`。

## 三、数据模型

### 3.1 新表与列

```sql
CREATE TABLE IF NOT EXISTS templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
);

-- 与现有 settings 表同形状（逐键 + JSON 值），仅多 template_id 作用域。
CREATE TABLE IF NOT EXISTS template_settings (
    template_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (template_id, key)
);

-- wallets 增列：绑定的模板；NULL = 默认模板（向后兼容）。
ALTER TABLE wallets ADD COLUMN template_id INTEGER;  -- 经 _migrate 加，默认 NULL
```

`settings` 表结构不变，职责收窄为「引擎级全局参数」。

### 3.2 读取语义（沿用现有逐键合并范式）

- `get_settings()`：保持现有签名与语义，但合并基准改为 `ENGINE_DEFAULTS`（只返回引擎级键）。
- `get_template(tid)`：`TEMPLATE_DEFAULTS` 合并 `template_settings` 中该模板的覆盖键 —— 与现有 `get_settings()` **逐字一样**的合并逻辑，只是加了 `template_id` 作用域。
- `get_template_for(wallet)`：取 `wallet["template_id"]`（NULL → 默认模板 id），再 `get_template(tid)`。
- 模板 CRUD：`create_template(name) -> id`、`save_template(tid, dict)`、`list_templates()`、`get_default_template_id()`、`set_wallet_template(address, tid)`、`delete_template(tid)`（默认模板不可删；删除时绑定它的钱包回落默认模板）。

### 3.3 迁移（`_migrate` 内，幂等）

沿用现有 `_migrate` 的 `PRAGMA table_info` + `ALTER` 先例。迁移分两段，均须可重入：

1. **建模板表 + 加 `wallets.template_id` 列**（若不存在）。
2. **数据迁移（仅当 `templates` 表为空时执行一次）**：
   - 建 `name="默认"` 模板，记其 id 为 `D`。
   - 从现有 `settings` 中把**策略级键**复制进 `template_settings(D, key, value)`。引擎级键留在 `settings`。
   - **把策略级键从 `settings` 删除**（不留影子，单一真相）。
   - `excluded_categories` 在默认模板中不存在 → 由 `TEMPLATE_DEFAULTS` 兜底为 `["sports","esports","weather"]`（无需写入）。
   - 所有 `wallets.template_id` 为 NULL 的钱包不动（NULL 已经语义指向默认模板）。

> 升级即用：用默认模板跑，**策略行为与升级前逐项一致**（唯一有意的差异见 §五 checkpoint ①）。

## 四、采集器拆分

```
        共享候选采集器（网络密集，用 1 个钱包的 API）
        · 读所有「在用模板」excluded_categories 的【交集】= 全局共同排除集
        · CLOB /rewards/markets/multi 抓【全量】          ┐
        · CLOB /rewards/markets/multi?tag_slug=<逐个排除品类> 抓【排除集】各自分页取全
        · 候选池 = 全量 − 排除集（按 condition_id 相减）  ┘
        · 给每条候选打 tags 标签（它属于哪些被查询过的品类）
        · 抓 spread + 完整订单簿并缓存
        · 【不算价】
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
        钱包A 按模板A精筛   钱包B 按模板B精筛   …（CPU 密集，各算各的）
        · 门槛过滤（奖励下限 / 价格区间 / 价差 / 结算天数 / 冷却 / 黑名单 / 持仓跳过）
        · 按【本模板】excluded_categories narrow 品类
        · place_orders：价格仍在下单时 live 重算【老】determine_order_price
```

**品类相减的两层逻辑（重要）**：

- 采集器按**所有在用模板排除集的交集**（= 所有模板都要排的品类）在源头排掉，缩小候选池而**不误删**任一模板可能需要的市场。
- 每钱包精筛时再按**本模板自己的排除集**narrow。

> 为什么是交集而非并集：若模板 A 排 `sports`、模板 B 不排，用并集会在源头删掉 B 需要的 sports 市场。交集（两者共同排的）在源头删才安全；模板独有的排除项留到每钱包精筛兜住。空交集 = 采集全部。

**给候选打品类标签**：采集器为了执行「全量 − 排除集」本就要逐个排除品类查询，把每条候选命中了哪些查询过的品类记进候选池 `tags` 字段，供每钱包精筛 narrow。

> 注意：采集器只查询「需要判定的品类」（= 所有模板排除集的并集，用于打标签 + 相减），不是全部品类。某市场若不属于任何被查询品类，`tags` 为空——它不会被任何模板的品类排除命中，符合黑名单语义。

### 4.1 候选池表（现有 `eligible_markets` 平移）

现有 `eligible_markets` 表语义平移为「候选池」。SP1 的调整：

- **新增列 `tags TEXT DEFAULT '[]'`**（JSON 数组，经 `_migrate` 加）。
- 采集器**不再写 `order_price`**（本就是废弃 / 展示值）。该列 `NOT NULL`，平移期写 0 占位；价格由每钱包 `place_orders` live 重算，候选池不承载价格。
- `min_cost`、`reward_range_*` 等沿用现有列（仍由采集器按市场算，与价格无关）。

### 4.2 `/api/eligible` 展示

SP1 保守处理：`/api/eligible` 展示**候选池**（模板无关，不按钱包细分）。按模板 / 按钱包细分的展示留给 SP6 UI。现有内存（扫描中）/ DB（空闲）双源逻辑不变。

## 五、配置读取点改造清单

把**策略级**读取点从 `get_settings()` 改为 `get_template_for(wallet)`；**引擎级**读取点仍走 `get_settings()`。

| 文件 / 位置 | 现读 | 改为 |
| --- | --- | --- |
| `engine/manager.py` `WalletWorker.__init__`（`settings` 快照） | `get_settings()` | 构造时传入 `get_template_for(wallet)`；引擎级 `fill_check_interval_sec` 仍来自 `get_settings()` |
| `engine/manager.py` `place_orders`（`max_buy_orders_per_wallet` / `order_size_mode` / `order_size_custom_usd`） | `db.get_settings()` | `db.get_template_for(self.wallet_address 对应钱包)` |
| `engine/monitor.py`（`stop_loss_pct` / `min_price_cents` / `max_price_cents`） | `settings[...]` | 该钱包模板 |
| `engine/scanner.py`（`min_reward_usd` / `min_price_cents` / `max_price_cents` / `max_spread_cents` / `min_settlement_days`） | `db.get_settings()` | **拆**：采集器读「所有在用模板的并集 / 交集」；每钱包精筛读该钱包模板 |
| `engine/order_sizing.py` | （纯函数，入参由 `place_orders` 喂） | 不改函数，改调用处传模板值 |
| `engine/manager.py` `_scanner_loop`（`scan_interval_sec`） | `get_settings()` | 不变（引擎级） |
| `web/routes.py:632`（`/api/...` 用 `stop_loss_pct`） | `db.get_settings()["stop_loss_pct"]` | 需按钱包取模板；该端点须带 wallet 参数（已有 `wallet = request.args.get("wallet")`） |
| `web/routes.py:247/254`（`/api/settings` GET/POST） | `db.get_settings()` / `save_settings` | 收窄为引擎级参数读写；模板读写走新端点（SP6 接 UI，SP1 仅留 DB 层方法 + 可选最小端点） |

> SP1 不引入新的引擎级参数读取点，也不改 `scan_interval` / `fill_check` / `cooldown` 的「全局生效」语义（每钱包级节奏属于 SP5）。

## 六、验收 checkpoint（成功标准）

1. **默认模板行为基线**：升级后用默认模板跑，扫描 / 下单 / 监控 / 止损行为与升级前**逐项一致**，唯一有意的差异是**体育（sports）/ 电竞（esports）/ 天气（weather）市场不再进候选池**。
2. **多模板隔离**：建第二个模板（如把 `max_spread_cents` 收窄、或 `excluded_categories` 改成只排 `weather`），绑到某钱包 → 该钱包精筛出的列表与默认钱包**明显不同**，且各自下单正确。
3. **共享采集 + 每钱包筛选**：一轮只发一次网络采集（CLOB 调用次数 = 1 全量 + N 排除品类，与钱包数无关），两个钱包各自 CPU 精筛。
4. **单元测试**（pytest，纯逻辑不触网）：
   - DB 层：模板 CRUD、`get_template` 合并、`get_template_for` 的 NULL→默认回落、`set_wallet_template`、删除默认模板被拒、删非默认时绑定钱包回落。
   - 迁移：在「旧库」（有 `settings`、无 `templates`）上跑 `_migrate` → 默认模板含全部策略键、`settings` 只剩引擎键、可重入（再跑一次不重复迁移 / 不报错）。
   - 品类相减：给定「全量」与「排除品类结果」的桩数据，候选池 = 全量 − 排除集，且 `tags` 标注正确；空排除集 = 全量；多模板交集 / 并集计算正确。

## 七、范围之外（明确不做）

- 不重写 `determine_order_price`（SP2）。
- 不改离场 / 止损算法（SP3）。
- 不加单份奖励阈值 / 取档（SP4）。
- 不改扫描节奏为三档、不加观察名单（SP5）。
- 不做模板管理前端（SP6）；SP1 只到 DB 层方法 + 必要的最小后端接线，前端沿用现有「全局参数」表单（其字段在 SP1 后语义上对应默认模板 / 引擎级，由 SP6 正式拆分 UI）。

## 八、待 SP4 前需验证的事实（不阻塞 SP1）

- 已确认品类 slug 与服务端 `tag_slug` 过滤可用（§一.2），SP4 的品类相关部分因此缩小为仅「单份奖励阈值 + 取档」。
