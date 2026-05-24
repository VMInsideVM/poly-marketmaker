# 下单量三模式 设计

## 背景与目标

当前买单数量写死为「全部可用余额能买到的份数」(`engine/manager.py:210-226`,`order_size = int(balance / order_price)`)。用户希望:

1. **默认改回**按「符合奖励条件的最小份额」挂单,而不是全额。
2. 把下单量做成**三种可切换**的全局模式:
   - `min`(默认):符合挂单奖励的最小份额。
   - `custom`:自定义「单个市场的挂单上限」——全局一个**美元**值,对每个市场都生效,份额 = `floor(上限 / 落单价)`。
   - `balance`:按余额的最大份额(即现有全额行为)。

非技术用户在设置页一个下拉框切换,custom 模式额外填一个美元上限。

## 现状(改前)

- scanner 给每个合格市场记 `order_size == rewards_min_size == min_size`(`engine/scanner.py:249,256`),并记一个 `min_cost = min_size × ceil_to_tick(reward_range_min)` 的每钱包余额门槛。
- `place_orders`(`engine/manager.py`):
  - 顶部已实时读 `self.db.get_settings()` 取 `max_buy_orders_per_wallet`(`manager.py:111`)——新设置项同样实时读,改设置下次下单生效、无需重启。
  - 168-175 行有 `min_cost` 余额门槛(`balance < min_cost` 跳过),三模式通用,保留。
  - 210-226 行是写死的全额反推 + 最小份额跳过,本次替换的就是这一段。
  - `_record_place_buy(market, order_price, order_size, ...)` 已接收真实下单份额,不改。
- 设置项:`settings` 表存用户覆盖,`get_settings()` 用 `DEFAULTS` 兜底合并(`config.py:6-18`、`models/database.py:151-166`)。
- 设置页 `config.html`:`loadSettings()` 按 `name` 把值塞进表单;**保存/输入处理对所有字段做 `parseFloat`**(`config.html:157,170`)——字符串型的模式字段会被转成 `NaN`,必须单独处理。

## 设计

### 1. 新增设置项(`config.py` `DEFAULTS`)

- `order_size_mode`:`"min"` | `"custom"` | `"balance"`,**默认 `"min"`**。
- `order_size_custom_usd`:浮点,默认 `0`,仅 `custom` 模式使用。

全局设置,对所有钱包、所有市场生效(与现有所有设置项一致,不引入 per-wallet/per-market 存储)。

### 2. 纯函数模块 `engine/order_sizing.py`

```python
def compute_order_size(mode, order_price, balance, min_size, custom_usd):
    """返回应下单的份额(int),或 None 表示跳过该市场。

    - min:    返回 min_size(恒满足奖励门槛;能否买得起由 place_orders 里
              已有的 min_cost 门槛在前面拦)。
    - custom: budget = min(custom_usd, balance)。
    - balance: budget = balance。
    - custom/balance:size = floor(budget / order_price);若 size < min_size
              则返回 None(挂了也吃不到奖励 -> 跳过)。
    - order_price <= 0 -> None;未知 mode -> 按 min 处理(安全兜底)。
    """
```

要点:

- `custom` 用 `min(custom_usd, balance)` 取,保证单笔买单不超过钱包真实余额(避免必然成交失败的超额单),同时保留 maker「多市场共用保证金、循环内不递减余额」的特性。
- `custom_usd <= 0` 时 budget 为 0,`size = 0 < min_size` 自然返回 None(不挂),UI 提示用户填写。
- `min_size` 由调用方按 `int(market.get("rewards_min_size", market.get("order_size", 0)) or 0)` 解析(沿用 220 行现有解析),`min` 模式返回它、`custom`/`balance` 模式用它做下限,单一来源。

### 3. 接入 `place_orders`(`engine/manager.py`)

- 在读 `max_buys` 附近(约 111 行)一并实时读 `mode = settings.get("order_size_mode", "min")`、`custom_usd = float(settings.get("order_size_custom_usd", 0) or 0)`。
- 把 210-226 行那段替换为:
  ```python
  balance = self.api.get_balance()
  min_size = int(market.get("rewards_min_size", market.get("order_size", 0)) or 0)
  order_size = compute_order_size(mode, order_price, balance, min_size, custom_usd)
  if order_size is None:
      logger.info(...)  # 记录跳过原因(模式/余额/上限)
      continue
  ```
  其余 `place_limit_buy(..., order_size, ...)` + `_record_place_buy(market, order_price, order_size, ...)` 不变。
- `min_cost` 门槛(168-175)保留,三模式通用。

### 4. 设置页 UI(`web/templates/config.html`)

- 新增「下单量」分组(放在「策略参数」或「运行参数」分组内):
  - `<select name="order_size_mode">`:`最小合格份额(默认)` = `min` / `自定义单市场美元上限` = `custom` / `按余额全额` = `balance`。
  - `<input type="number" name="order_size_custom_usd" step="1" min="0">`:标签注明「仅自定义模式生效,单个市场单笔买单最多投入的美元」。
- 修 JS:`submit` 与 `input` 两个处理器里,`order_size_mode` 保留为字符串,其余字段仍 `parseFloat`。`loadSettings()` 对 `<select>` 直接 `input.value = data[key]` 即可正确选中(无需特殊处理)。
- `beforeunload`/导航离开的「未保存」比对逻辑沿用,字符串字段一致比较即可。

### 5. 测试

- **新建 `tests/test_order_sizing.py`**(纯逻辑,不触网):
  - `min` 模式返回 `min_size`(与余额、价格无关)。
  - `balance` 模式 = `floor(balance / price)`。
  - `custom` 模式 = `floor(min(custom_usd, balance) / price)`。
  - `custom` 上限大于余额时被余额夹住。
  - `custom`/`balance` 算出的份额 `< min_size` 时返回 `None`。
  - `custom_usd <= 0` 返回 `None`。
  - `order_price <= 0` 返回 `None`。
  - 未知 mode 按 `min` 处理。
- **改 `tests/test_place_orders.py`**:
  - `_worker` 默认在 `db.get_settings.return_value` 里种 `order_size_mode: "min"`(对齐生产默认)。
  - 现有 4 个全额测试(`test_order_size_uses_full_balance` 等)显式把模式设为 `"balance"`,断言不变。
  - 补一个回归:默认 min 模式下单份额 = `min_size`(即 `_market` 的 `order_size`)。

## 取舍

- `custom` 上限取**美元**而非份额(用户确认):每市场单笔最多投这么多钱,因价格不同实际份数不同。
- `custom` 被余额反向夹住,而非「上限即上限、超过余额也挂」:避免单笔必然成交失败的超额单。
- 低于奖励最小份额时**跳过**该市场,而非强行补到 `min_size`:补到 min 会突破用户设的 custom 上限,语义矛盾;跳过与现有全额模式行为一致。

## 非目标(YAGNI)

- 不做 per-market 单独上限(用户已选全局美元上限)。
- 不做 per-wallet 不同模式。
- 不改 scanner 的 `order_size`/`min_cost` 记录,不改监控 Step3 重挂(用 `original_size` 自动沿用既有份额)。
