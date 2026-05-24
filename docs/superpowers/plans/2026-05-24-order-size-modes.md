# 下单量三模式 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把买单数量从写死的「全额余额」改成三种可切换的全局模式——`min`(符合奖励的最小份额,默认)/`custom`(自定义单市场美元上限)/`balance`(全额),用户在设置页一个下拉框切换。

**Architecture:** 新建纯函数 `engine/order_sizing.py::compute_order_size` 承载三模式分支(配独立单测);`config.py` 加两个设置项默认值;`place_orders` 实时读设置并调用该函数决定份额或跳过;设置页加下拉框 + 自定义美元上限输入,并修掉「保存时对所有字段 `parseFloat`」会把字符串模式转成 `NaN` 的坑。

**Tech Stack:** Python 3、pytest、unittest.mock(纯逻辑不触网);前端为 Flask 模板 + 原生 JS。

参考 spec:`docs/superpowers/specs/2026-05-24-order-size-modes-design.md`

---

## File Structure

- Create: `engine/order_sizing.py` — 纯函数 `compute_order_size(mode, order_price, balance, min_size, custom_usd)`,唯一职责是「按模式算份额或返回 None 跳过」。
- Create: `tests/test_order_sizing.py` — 上述纯函数的单测。
- Modify: `config.py` — `DEFAULTS` 加 `order_size_mode` / `order_size_custom_usd`。
- Modify: `models` 无改动;`tests/test_database.py` — 加一条「`get_settings()` 含新默认值」的回归。
- Modify: `engine/manager.py` — 顶部 import;`place_orders` 顶部读设置 + 替换写死的全额下单段。
- Modify: `tests/test_place_orders.py` — `_worker`/`_worker_capped` 种 `order_size_mode:"min"` 默认;现有 5 个全额测试显式改 `"balance"`;补 min 模式与 custom 模式的集成测试。
- Modify: `web/templates/config.html` — 加下拉框 + 自定义美元上限输入,修保存/输入 JS 对字符串字段的处理。

每个 Task 提交时只 stage 本任务涉及的文件(项目约定:别带进未提交的其它 WIP)。

---

### Task 1: 纯函数 `compute_order_size`

**Files:**
- Create: `engine/order_sizing.py`
- Test: `tests/test_order_sizing.py`

- [ ] **Step 1: 写失败测试**

新建 `tests/test_order_sizing.py`:

```python
"""tests/test_order_sizing.py"""

from engine.order_sizing import compute_order_size


def test_min_mode_returns_min_size_regardless_of_balance_and_price():
    assert compute_order_size("min", 0.50, 1000.0, 10, 0.0) == 10
    assert compute_order_size("min", 0.99, 5.0, 7, 0.0) == 7


def test_balance_mode_floors_balance_over_price():
    assert compute_order_size("balance", 0.50, 1000.0, 10, 0.0) == 2000


def test_balance_mode_floors_inexact_division():
    # 除不尽向下取整(取整向上会在成交时超出余额)。
    assert compute_order_size("balance", 0.30, 1000.0, 10, 0.0) == 3333


def test_balance_mode_skips_when_below_min_size():
    # floor(3.0/0.50)=6 < min_size 10 -> None
    assert compute_order_size("balance", 0.50, 3.0, 10, 0.0) is None


def test_custom_mode_floors_cap_over_price():
    # cap 50 美元,余额充足 -> floor(50/0.50)=100
    assert compute_order_size("custom", 0.50, 1000.0, 10, 50.0) == 100


def test_custom_mode_capped_by_balance_when_cap_exceeds_balance():
    # cap 50 但余额只有 20 -> 用 20:floor(20/0.50)=40
    assert compute_order_size("custom", 0.50, 20.0, 10, 50.0) == 40


def test_custom_mode_skips_when_below_min_size():
    # cap 4 美元 -> floor(4/0.50)=8 < min_size 10 -> None
    assert compute_order_size("custom", 0.50, 1000.0, 10, 4.0) is None


def test_custom_mode_zero_cap_skips():
    # cap 0 -> 预算 0 -> size 0 < min_size -> None(不挂)
    assert compute_order_size("custom", 0.50, 1000.0, 10, 0.0) is None


def test_non_positive_price_returns_none():
    assert compute_order_size("balance", 0.0, 1000.0, 10, 0.0) is None
    assert compute_order_size("custom", -0.10, 1000.0, 10, 50.0) is None


def test_unknown_mode_falls_back_to_min():
    assert compute_order_size("weird", 0.50, 1000.0, 10, 50.0) == 10
```

- [ ] **Step 2: 运行测试,确认失败**

Run: `pytest tests/test_order_sizing.py -v`
Expected: 全部 FAIL，报 `ModuleNotFoundError: No module named 'engine.order_sizing'`。

- [ ] **Step 3: 写实现**

新建 `engine/order_sizing.py`:

```python
"""下单量计算:按模式决定每笔买单挂多少份。

纯函数,不触网。place_orders 读设置后按市场调用。
"""


def compute_order_size(mode, order_price, balance, min_size, custom_usd):
    """返回应下单的份额(int),或 None 表示跳过该市场。

    - "min":     返回 min_size。恒满足奖励门槛;能否买得起由 place_orders
                 里已有的 min_cost 门槛在前面拦,这里不再判余额。
    - "custom":  预算 = min(custom_usd, balance),按美元上限下单但不超过余额。
    - "balance": 预算 = balance,全额下单。
    - "custom"/"balance":size = floor(预算 / order_price);若 size < min_size
                 则返回 None(份额不够拿奖励,挂了也吃不到 -> 跳过)。
    - order_price <= 0 -> None;未知 mode -> 按 "min" 处理(安全兜底)。
    """
    if mode not in ("custom", "balance"):
        # "min" 和任何未知模式都按最小合格份额处理(安全兜底)。
        return min_size
    if order_price <= 0:
        return None
    budget = balance if mode == "balance" else min(custom_usd, balance)
    size = int(budget / order_price)
    if size < min_size:
        return None
    return size
```

- [ ] **Step 4: 运行测试,确认通过**

Run: `pytest tests/test_order_sizing.py -v`
Expected: 10 个全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add engine/order_sizing.py tests/test_order_sizing.py
git commit -m "feat: compute_order_size 纯函数(min/custom/balance 三模式下单量)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: 设置项默认值

**Files:**
- Modify: `config.py:17`(`DEFAULTS` 末尾)
- Test: `tests/test_database.py`(`TestSettings` 类内)

- [ ] **Step 1: 写失败测试**

在 `tests/test_database.py` 的 `class TestSettings` 内、`test_get_default_settings` 之后追加:

```python
    def test_get_settings_includes_order_size_defaults(self, db):
        settings = db.get_settings()
        assert settings["order_size_mode"] == "min"
        assert settings["order_size_custom_usd"] == 0.0
```

- [ ] **Step 2: 运行测试,确认失败**

Run: `pytest tests/test_database.py::TestSettings::test_get_settings_includes_order_size_defaults -v`
Expected: FAIL，`KeyError: 'order_size_mode'`(`DEFAULTS` 还没这两个键)。

- [ ] **Step 3: 改 `config.py`**

把 `config.py` 的 `DEFAULTS` 这一行(`config.py:17`):

```python
    "max_buy_orders_per_wallet": 5,
}
```

替换为:

```python
    "max_buy_orders_per_wallet": 5,
    "order_size_mode": "min",
    "order_size_custom_usd": 0.0,
}
```

- [ ] **Step 4: 运行测试,确认通过**

Run: `pytest tests/test_database.py::TestSettings -v`
Expected: 全部 PASS(新增 1 个 + 原有 settings 测试不受影响)。

- [ ] **Step 5: 提交**

```bash
git add config.py tests/test_database.py
git commit -m "feat: 设置项 order_size_mode/order_size_custom_usd 默认值(默认 min)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: place_orders 接入三模式

**Files:**
- Modify: `engine/manager.py`(顶部 import;`place_orders` 约 `111` 行读设置;约 `210-226` 行替换全额下单段)
- Test: `tests/test_place_orders.py`

说明(给实现者):
- `place_orders` 已在约 111 行用 `self.db.get_settings()` 实时读 `max_buy_orders_per_wallet`,本任务把它改成「读一次 settings 复用」,顺带取出模式与美元上限。改设置下次下单生效、无需重启。
- `_record_place_buy(market, order_price, order_size, ...)` 已接收真实下单份额,**不改**。
- `place_limit_buy` 在测试里按位置参数调用,下单份额是 `call_args.args[2]`。
- `compute_order_size` 已在 Task 1 完成并测过,这里只做接线与集成测试。

- [ ] **Step 1: 改测试 —— 默认改成 min 模式,现有全额测试显式标 balance,补两个集成测试**

(a) 在 `tests/test_place_orders.py` 的 `_worker`(约 10-13 行)的设置字典里加 `order_size_mode`:

把:

```python
    db.get_settings.return_value = {
        "max_buy_orders_per_wallet": cap,
        "cooldown_minutes": 20,
    }
```

替换为:

```python
    db.get_settings.return_value = {
        "max_buy_orders_per_wallet": cap,
        "cooldown_minutes": 20,
        "order_size_mode": "min",
    }
```

(b) 同样地,在 `_worker_capped`(约 69-72 行)的设置字典里加 `"order_size_mode": "min",`:

把:

```python
    db.get_settings.return_value = {
        "max_buy_orders_per_wallet": cap,
        "cooldown_minutes": 20,
    }
```

替换为:

```python
    db.get_settings.return_value = {
        "max_buy_orders_per_wallet": cap,
        "cooldown_minutes": 20,
        "order_size_mode": "min",
    }
```

(c) 给现有 5 个「全额」测试显式切到 balance 模式。对下面每个测试,在 `worker = _worker(api, db)` 那行**之后**插入一行:

```python
    db.get_settings.return_value["order_size_mode"] = "balance"
```

需要插入的测试(它们断言的份额 2000/3333 都依赖全额):
- `test_order_size_uses_full_balance`
- `test_balance_not_decremented_across_markets`
- `test_skip_when_full_balance_below_min_reward_size`
- `test_place_buy_action_records_full_size`
- `test_order_size_floors_on_inexact_division`

(d) 在文件**末尾**追加 min 模式与 custom 模式两个集成测试:

```python
def test_min_mode_places_reward_min_size_by_default():
    # 默认 min 模式:下单份额 = 最小合格份额(_market 的 order_size=10),与余额无关。
    api = MagicMock()
    db = MagicMock()
    db.is_in_cooldown.return_value = False
    api.get_open_orders.return_value = []
    api.get_orderbook.return_value = _ok_orderbook()
    api.get_balance.return_value = 1000.0
    worker = _worker(api, db)  # 默认 order_size_mode == "min"
    with patch("engine.strategy.determine_order_price", return_value=0.50):
        worker.place_orders([_market(0)])
    api.place_limit_buy.assert_called_once()
    assert api.place_limit_buy.call_args.args[2] == 10  # min_size,不是 2000


def test_custom_mode_uses_dollar_cap():
    # custom 模式:每市场单笔按 order_size_custom_usd 美元上限下单。
    api = MagicMock()
    db = MagicMock()
    db.is_in_cooldown.return_value = False
    api.get_open_orders.return_value = []
    api.get_orderbook.return_value = _ok_orderbook()
    api.get_balance.return_value = 1000.0
    worker = _worker(api, db)
    db.get_settings.return_value["order_size_mode"] = "custom"
    db.get_settings.return_value["order_size_custom_usd"] = 50.0
    with patch("engine.strategy.determine_order_price", return_value=0.50):
        worker.place_orders([_market(0)])
    api.place_limit_buy.assert_called_once()
    assert api.place_limit_buy.call_args.args[2] == 100  # floor(50/0.50)
```

- [ ] **Step 2: 运行测试,确认失败**

Run: `pytest tests/test_place_orders.py -k "min_mode or custom_mode_uses_dollar_cap" -v`
Expected: 2 个新测试 FAIL —— 当前 `place_orders` 写死全额,min 模式会下 2000(断言 10 失败),custom 模式也下 2000(断言 100 失败)。

- [ ] **Step 3: 顶部加 import**

在 `engine/manager.py` 第 15 行 `from engine.positions import held_condition_ids` 之后加一行:

```python
from engine.order_sizing import compute_order_size
```

- [ ] **Step 4: place_orders 顶部读一次设置**

把 `engine/manager.py` 约 111 行这一句:

```python
        max_buys = int(self.db.get_settings().get("max_buy_orders_per_wallet", 5))
```

替换为:

```python
        settings = self.db.get_settings()
        max_buys = int(settings.get("max_buy_orders_per_wallet", 5))
        order_size_mode = settings.get("order_size_mode", "min")
        order_size_custom_usd = float(settings.get("order_size_custom_usd", 0) or 0)
```

- [ ] **Step 5: 替换写死的全额下单段**

把 `engine/manager.py` 约 210-226 行这一段:

```python
            balance = self.api.get_balance()
            # 每笔买单按全部可用余额下单。Polymarket maker 买单不锁仓,同一笔
            # 余额垫付所有挂着的买单,所以这里跨市场循环时刻意 *不* 递减余额。
            order_size = int(balance / order_price)
            # 全额都买不够拿奖励的最小合格份额,挂了也吃不到奖励 -> 跳过该市场。
            min_size = int(
                market.get("rewards_min_size", market.get("order_size", 0)) or 0
            )
            if order_size < min_size:
                logger.info(
                    "Balance %.2f only buys %d < min reward size %d for %s, skip",
                    balance,
                    order_size,
                    min_size,
                    market["market_name"],
                )
                continue
```

替换为:

```python
            balance = self.api.get_balance()
            # 下单份额按设置的模式决定(min=最小合格份额 / custom=美元上限 /
            # balance=全额)。maker 买单不锁仓,同一笔余额垫付所有挂着的买单,
            # 所以跨市场循环时刻意 *不* 递减余额。
            min_size = int(
                market.get("rewards_min_size", market.get("order_size", 0)) or 0
            )
            order_size = compute_order_size(
                order_size_mode,
                order_price,
                balance,
                min_size,
                order_size_custom_usd,
            )
            if order_size is None:
                logger.info(
                    "Skip %s: mode=%s balance=%.2f cap=%.2f price=%.4f "
                    "buys fewer than min reward size %d",
                    market["market_name"],
                    order_size_mode,
                    balance,
                    order_size_custom_usd,
                    order_price,
                    min_size,
                )
                continue
```

(下方 `try:` / `place_limit_buy(..., order_size, ...)` / `_record_place_buy(market, order_price, order_size, ...)` 均不变。)

- [ ] **Step 6: 运行新测试,确认通过**

Run: `pytest tests/test_place_orders.py -k "min_mode or custom_mode_uses_dollar_cap" -v`
Expected: 2 个全部 PASS。

- [ ] **Step 7: 跑整个文件,确认无回归**

Run: `pytest tests/test_place_orders.py -v`
Expected: 全部 PASS —— 5 个全额测试因显式标了 `"balance"` 仍断言 2000/3333;只校验 `call_count`/`assert_called`/`assert_not_called` 的测试(cap/limit/黑名单/持仓/min_cost)在 min 模式下下单份额=10,数量行为不变。

- [ ] **Step 8: 跑全套测试**

Run: `pytest`
Expected: 全部 PASS。

- [ ] **Step 9: 提交**

```bash
git add engine/manager.py tests/test_place_orders.py
git commit -m "feat: place_orders 按 order_size_mode 三模式下单量(默认 min)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: 设置页 UI(下拉框 + 美元上限 + JS 修字符串)

**Files:**
- Modify: `web/templates/config.html`(约 73-74 行插入新分组;约 153-171 行修 JS)

说明:模板/JS 本项目无自动化测试,本任务用手动验证收尾。务必照抄下面 HTML 与 JS,字符串模式字段不能被 `parseFloat` 转成 `NaN`。

- [ ] **Step 1: 加「下单量」分组**

在 `web/templates/config.html` 中,把「运行参数」grid 结束到保存按钮这一段(约 73-74 行):

```html
        </div>
        <button type="submit" class="btn btn-primary">保存设置</button>
```

替换为(在 `</div>` 与按钮之间插入新分组):

```html
        </div>

        <h2>下单量</h2>
        <div class="form-grid">
            <div class="form-group">
                <label>下单量模式</label>
                <select name="order_size_mode">
                    <option value="min">符合奖励的最小份额（默认）</option>
                    <option value="custom">自定义单市场美元上限</option>
                    <option value="balance">按余额全额</option>
                </select>
            </div>
            <div class="form-group">
                <label>自定义美元上限 (USDC，仅“自定义单市场美元上限”模式生效)</label>
                <input type="number" name="order_size_custom_usd" step="1" min="0">
            </div>
        </div>
        <button type="submit" class="btn btn-primary">保存设置</button>
```

- [ ] **Step 2: 修 submit 处理器(字符串模式不 parseFloat)**

把 `web/templates/config.html` 约 155-157 行:

```javascript
    const formData = new FormData(this);
    const data = {};
    formData.forEach((val, key) => { data[key] = parseFloat(val); });
```

替换为:

```javascript
    const formData = new FormData(this);
    const data = {};
    formData.forEach((val, key) => {
        data[key] = (key === 'order_size_mode') ? val : parseFloat(val);
    });
```

- [ ] **Step 3: 修 input 处理器(同样保留字符串)**

把 `web/templates/config.html` 约 168-171 行:

```javascript
document.getElementById('settings-form').addEventListener('input', function() {
    const formData = new FormData(this);
    formData.forEach((val, key) => { currentSettings[key] = parseFloat(val); });
});
```

替换为:

```javascript
document.getElementById('settings-form').addEventListener('input', function() {
    const formData = new FormData(this);
    formData.forEach((val, key) => {
        currentSettings[key] = (key === 'order_size_mode') ? val : parseFloat(val);
    });
});
```

(`loadSettings()` 的 `input.value = data[key]` 对 `<select>` 直接赋 value 即可正确选中,无需改动。)

- [ ] **Step 4: 手动验证**

1. `python app.py`,登录后打开设置页(`/config`)。
2. 确认「下单量」分组出现:下拉框默认选中「符合奖励的最小份额（默认）」,下面是美元上限输入框。
3. 切到「自定义单市场美元上限」,美元上限填 `50`,点「保存设置」,看到保存成功提示。
4. 刷新页面,确认下拉框仍停在「自定义单市场美元上限」、美元上限仍是 `50`(说明字符串模式没被存成 `NaN`/`null`)。
5. 打开浏览器开发者工具 Network,保存时看 `POST /api/settings` 的 body:`order_size_mode` 是字符串 `"custom"`、`order_size_custom_usd` 是数字 `50`。

- [ ] **Step 5: 提交**

```bash
git add web/templates/config.html
git commit -m "feat: 设置页下单量模式下拉框 + 自定义美元上限(修字符串字段被 parseFloat)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

- **Spec 覆盖**:
  - 三模式 `min`/`custom`/`balance` → Task 1 `compute_order_size` + Task 3 接线 ✓
  - 默认 `min` → Task 2 `DEFAULTS["order_size_mode"]="min"` + Task 3 `_worker` 默认 + `test_min_mode_places_reward_min_size_by_default` ✓
  - `custom` 全局美元上限、份额=floor(上限/价) → Task 1 `test_custom_mode_floors_cap_over_price` + Task 3 `test_custom_mode_uses_dollar_cap` ✓
  - `custom` 被余额夹住 → Task 1 `test_custom_mode_capped_by_balance_when_cap_exceeds_balance` ✓
  - `custom`/`balance` 低于 min_size 跳过 → Task 1 两个 skip 测试 ✓
  - `custom_usd<=0` 不挂 → Task 1 `test_custom_mode_zero_cap_skips` ✓
  - `price<=0` 兜底、未知 mode 按 min → Task 1 `test_non_positive_price_returns_none` / `test_unknown_mode_falls_back_to_min` ✓
  - 实时读设置、改设置下次生效 → Task 3 Step 4(读 `self.db.get_settings()`,不缓存)✓
  - `min_cost` 门槛保留、三模式通用 → Task 3 未触碰 168-175 行,原有 `test_skips_market_when_balance_below_min_cost` 保护 ✓
  - 设置页下拉 + 美元上限 + 字符串不被 parseFloat → Task 4 ✓
  - 不改 scanner / 监控 Step3 / `_record_place_buy` → 这些文件不在改动清单 ✓
- **占位符扫描**:无 TBD/TODO,每个代码步骤都给了完整代码;Task 4 因无 JS 测试框架以手动验证收尾(已列具体步骤)。✓
- **类型/签名一致**:`compute_order_size(mode, order_price, balance, min_size, custom_usd)` 在 Task 1 定义、Task 3 Step 5 按此顺序调用一致;返回 `int | None`,Task 3 用 `if order_size is None: continue` 对应。✓
- **命名一致**:设置键 `order_size_mode` / `order_size_custom_usd` 在 config(Task 2)、manager(Task 3)、模板 `name=`(Task 4)、测试里全程同名。✓
