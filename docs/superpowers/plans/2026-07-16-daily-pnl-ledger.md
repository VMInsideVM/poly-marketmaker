# 每日做市盈亏台账 Implementation Plan（子项目1）

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development 或 executing-plans。Steps 用 `- [ ]`。
> Phase 0（探针 `probe_pnl.py`）已完成，字段形态已锁定（见 spec §十四）。本计划实现 Phase 1-5。

**Goal:** 把净值曲线扩成每日盈亏台账：按天分列做市奖励/返佣/卖出盈利/亏损/手续费/净利润，补漏（从 2026-06-01）、过滤充提、全钱包汇总、本地可视化。

**Architecture:** 公开 `/activity`（奖励 REWARD、返佣 MAKER_REBATE、赎回 REDEEM 骨架）+ authed `get_trades`（成交/手续费，复用 `extract_fills`+FIFO）→ 纯函数 `engine/pnl.py` 按北京日归集 → `daily_pnl` 表 upsert → 编排 `engine/pnl_ledger.py`（启动补漏+每日更新）→ `/api/pnl` + 前端。

**Tech Stack:** Python 3、Flask、SQLite、pytest；前端 Jinja+内联 JS（含中文，主会话手改）。

## Global Constraints（spec 锁定，逐 task 适用）

- **口径**：`net_usd = reward_usd + rebate_usd + sell_profit_usd − loss_usd − fee_usd`。奖励=公开 `/activity` REWARD.`usdcSize`；返佣=`/activity` MAKER_REBATE.`usdcSize`；卖出盈亏+手续费=authed `get_trades`（FIFO）；结算盈亏 v1 先不计（无样本）。
- **时区**：北京 `+8h` 分天，不靠机器时区。奖励 `reward_date = beijing_day(timestamp) − 1 天`；返佣/成交 `beijing_day(timestamp/match_time)`。
- **成本铁律**：只认 `get_trades` FIFO 重建，**禁 Data API `avgPrice`/`curPrice`**（[[take-profit-position-driven]]）。
- **手续费**：`fee = (fee_rate_bps/10000) × price × size`（实测全 0；公式未经非零验证，注明）。maker 恒 0。
- **补漏起点 2026-06-01**；幂等 upsert（主键 wallet+date）；近 3 天每轮重算。
- **持久化**：`%LOCALAPPDATA%` 同一 sqlite，升级不丢（现有存储已满足）；`net_worth_history` 不动。
- 含中文前端主会话手改（subagent 易别字+BOM）；文案简体中文。

---

## Phase 1：API 原语 `/activity`

### Task 1.1: `get_activity`（`api/polymarket_api.py`）

**Files:** Modify `api/polymarket_api.py`（`get_user_positions` 附近）；Test `tests/test_activity_api.py`（新建）。

**Interfaces:** Produces `get_activity(self, types=None, start=None, end=None) -> list[dict]`（公开 `/activity?user=funder`，offset 翻页至末页）。

- [ ] **Step 1: 失败测试** `tests/test_activity_api.py`：

```python
from unittest.mock import patch, MagicMock
from api.polymarket_api import PolymarketAPI


def _api():
    api = PolymarketAPI.__new__(PolymarketAPI)  # 不走 __init__（免私钥/网络）
    api.get_funder = lambda: "0xFUND"
    api.proxy_url = None
    return api


def _resp(items):
    m = MagicMock()
    m.json.return_value = items
    m.raise_for_status.return_value = None
    return m


def test_get_activity_paginates_until_short_page():
    api = _api()
    page1 = [{"type": "REWARD", "usdcSize": 1.0, "timestamp": 1} for _ in range(500)]
    page2 = [{"type": "TRADE", "usdcSize": 2.0, "timestamp": 2}]
    with patch("api.polymarket_api.http_get", side_effect=[_resp(page1), _resp(page2)]) as g:
        out = api.get_activity()
    assert len(out) == 501
    assert g.call_count == 2  # 满页续拉、短页停


def test_get_activity_passes_type_and_window():
    api = _api()
    with patch("api.polymarket_api.http_get", return_value=_resp([])) as g:
        api.get_activity(types=["REWARD", "MAKER_REBATE"], start=100, end=200)
    params = g.call_args.kwargs["params"]
    assert params["user"] == "0xFUND"
    assert params["type"] == "REWARD,MAKER_REBATE"
    assert params["start"] == 100 and params["end"] == 200
```

- [ ] **Step 2:** `pytest tests/test_activity_api.py -v` → FAIL（无 `get_activity`）。

- [ ] **Step 3: 实现**（`api/polymarket_api.py`，`get_user_positions` 之后）：

```python
    def get_activity(self, types=None, start=None, end=None) -> list:
        """Data API /activity（公开，按 funder）。offset 翻页至末页。

        types: 可选 type 白名单（逗号拼接）;start/end: epoch 秒窗口。用于盈亏台账取
        奖励(REWARD)/返佣(MAKER_REBATE)/赎回(REDEEM)。走本钱包代理(self.proxy_url)。
        """
        user = self.get_funder()
        limit = 500
        offset = 0
        out: list = []
        while True:
            params = {"user": user, "limit": limit, "offset": offset}
            if types:
                params["type"] = ",".join(types)
            if start is not None:
                params["start"] = int(start)
            if end is not None:
                params["end"] = int(end)
            resp = http_get(f"{DATA_API_HOST}/activity", params=params, timeout=20)
            resp.raise_for_status()
            page = resp.json()
            if not isinstance(page, list) or not page:
                break
            out.extend(page)
            if len(page) < limit or offset + limit >= 10000:  # 末页 / offset 上限
                break
            offset += limit
        return out
```

（`get_activity` 是实例方法，`_PROXIED_METHODS` 需加 `"get_activity"` 使其自 route 代理——检查该 loop 并追加。）

- [ ] **Step 4:** `pytest tests/test_activity_api.py -v` → PASS。
- [ ] **Step 5:** `pytest -q` → 全绿。
- [ ] **Step 6: 提交** `git add api/polymarket_api.py tests/test_activity_api.py && git commit`（`feat(api): get_activity 拉 /activity（奖励/返佣/赎回）`）。

---

## Phase 2：纯计算 `engine/pnl.py` + `extract_fills` 带费率

### Task 2.1: `extract_fills` 增 `fee_rate_bps`（`engine/fills.py`）

**Files:** Modify `engine/fills.py` `extract_fills`；Test `tests/test_fills.py`（追加）。

**Interfaces:** `extract_fills` 每条 fill 增 `"fee_rate_bps": float`（maker 取 mo、taker 取 top-level，空/缺=0）；现有键不变。

- [ ] **Step 1: 失败测试**（`tests/test_fills.py` 追加）：

```python
def test_extract_fills_carries_fee_rate_bps():
    from engine.fills import extract_fills
    trades = [{
        "id": "t1", "match_time": "100", "trader_side": "TAKER",
        "side": "SELL", "asset_id": "A", "size": "10", "price": "0.2",
        "fee_rate_bps": "30", "maker_orders": [],
    }, {
        "id": "t2", "match_time": "200", "trader_side": "MAKER",
        "asset_id": "X", "maker_orders": [
            {"maker_address": "0xFUND", "asset_id": "A", "side": "BUY",
             "matched_amount": "5", "price": "0.1", "fee_rate_bps": ""}],
    }]
    fills = extract_fills(trades, "0xFUND", "A")
    taker = [f for f in fills if f["trade_id"] == "t1"][0]
    maker = [f for f in fills if f["trade_id"] == "t2"][0]
    assert taker["fee_rate_bps"] == 30.0        # taker 取 top-level
    assert maker["fee_rate_bps"] == 0.0         # maker 空串 -> 0
    # 既有键不变
    assert taker["side"] == "SELL" and taker["price"] == 0.2 and taker["size"] == 10.0
```

- [ ] **Step 2:** `pytest tests/test_fills.py -k fee_rate -v` → FAIL（无该键）。
- [ ] **Step 3: 实现**：`extract_fills` 里 maker 分支 append 加 `"fee_rate_bps": float(mo.get("fee_rate_bps") or 0)`，taker 分支加 `"fee_rate_bps": float(tr.get("fee_rate_bps") or 0)`（空串 `or 0` → 0）。
- [ ] **Step 4-5:** 该测试 + `pytest -q` 全绿（既有 `extract_fills`/`position_cost_with_lots` 用例不受影响——只加键）。
- [ ] **Step 6:** commit（`feat(fills): extract_fills 携带 fee_rate_bps`）。

### Task 2.2: `engine/pnl.py` 时间/奖励/手续费 纯函数

**Files:** Create `engine/pnl.py`；Test `tests/test_pnl.py`（新建）。

**Interfaces:** Produces `beijing_day(ts)`, `fee_from_fill(fill)`, `reward_rebate_by_day(activity)`, `our_traded_assets(trades, funder)`。

- [ ] **Step 1: 失败测试** `tests/test_pnl.py`：

```python
from engine.pnl import (
    beijing_day, fee_from_fill, reward_rebate_by_day, our_traded_assets,
)

# 北京日 = UTC+8。UTC 2026-01-01 15:59:59 -> 北京 23:59:59 同日;16:00:00 -> 次日 00:00。
def test_beijing_day_boundary():
    assert beijing_day(1735747199) == "2026-01-01"   # UTC 2026-01-01 15:59:59
    assert beijing_day(1735747200) == "2026-01-02"   # UTC 2026-01-01 16:00:00

def test_fee_from_fill_zero_and_nonzero():
    assert fee_from_fill({"fee_rate_bps": 0.0, "price": 0.2, "size": 10}) == 0.0
    # 30 bps × 0.2 × 10 = 0.006
    assert abs(fee_from_fill({"fee_rate_bps": 30.0, "price": 0.2, "size": 10}) - 0.006) < 1e-9

def test_reward_rebate_by_day():
    # REWARD 发放时刻 UTC 2026-01-02 00:00:00(北京 01-02 08:00)-> 归 earning 日 01-01
    # MAKER_REBATE 时刻 UTC 2026-01-02 00:00:00(北京 01-02)-> 归 01-02
    acts = [
        {"type": "REWARD", "usdcSize": 7.0, "timestamp": 1735776000},
        {"type": "MAKER_REBATE", "usdcSize": 0.5, "timestamp": 1735776000},
        {"type": "DEPOSIT", "usdcSize": 100.0, "timestamp": 1735776000},  # 忽略
    ]
    out = reward_rebate_by_day(acts)
    assert out["2026-01-01"]["reward"] == 7.0
    assert out["2026-01-02"]["rebate"] == 0.5
    assert "2026-01-01" not in [d for d, v in out.items() if v.get("rebate")]

def test_our_traded_assets():
    trades = [
        {"trader_side": "TAKER", "asset_id": "A", "maker_orders": []},
        {"trader_side": "MAKER", "asset_id": "Z",
         "maker_orders": [{"maker_address": "0xFUND", "asset_id": "B"}]},
    ]
    assert our_traded_assets(trades, "0xFUND") == {"A", "B"}
```

- [ ] **Step 2:** `pytest tests/test_pnl.py -v` → FAIL。
- [ ] **Step 3: 实现** `engine/pnl.py`：

```python
"""engine/pnl.py — 每日盈亏台账纯计算（无 IO，全单测）。"""

from datetime import datetime, timezone, timedelta

_BJ = timezone(timedelta(hours=8))


def beijing_day(ts) -> str:
    """epoch 秒 -> 北京(UTC+8)日期 'YYYY-MM-DD'（固定偏移，不靠机器时区）。"""
    return datetime.fromtimestamp(float(ts or 0), _BJ).strftime("%Y-%m-%d")


def _prev_day(date_str: str) -> str:
    return (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")


def fee_from_fill(fill) -> float:
    """taker 手续费（美元）。实测 fee_rate_bps 全 0 -> 0;公式备将来收费（未经非零验证）。"""
    bps = float(fill.get("fee_rate_bps", 0) or 0)
    if bps <= 0:
        return 0.0
    return (bps / 10000.0) * float(fill.get("price", 0) or 0) * float(fill.get("size", 0) or 0)


def reward_rebate_by_day(activity) -> dict:
    """/activity -> {date: {"reward": x, "rebate": y}}。
    REWARD 归 beijing_day(ts)-1（发放前一天=earning 日）;MAKER_REBATE 归 beijing_day(ts)。
    其余 type（TRADE/DEPOSIT/WITHDRAWAL/REDEEM/…）此函数忽略。"""
    out: dict = {}
    for a in activity or []:
        t = str(a.get("type", "")).upper()
        amt = float(a.get("usdcSize", 0) or 0)
        ts = a.get("timestamp", 0)
        if t == "REWARD":
            d = _prev_day(beijing_day(ts))
            out.setdefault(d, {}).setdefault("reward", 0.0)
            out[d]["reward"] += amt
        elif t == "MAKER_REBATE":
            d = beijing_day(ts)
            out.setdefault(d, {}).setdefault("rebate", 0.0)
            out[d]["rebate"] += amt
    return out


def our_traded_assets(trades, funder) -> set:
    """我方成交涉及的全部 asset_id（taker=top-level asset;maker=maker_orders 内我方条目）。"""
    f = (funder or "").lower()
    assets = set()
    for tr in trades:
        we_maker = False
        for mo in tr.get("maker_orders", []) or []:
            if str(mo.get("maker_address", "")).lower() == f:
                we_maker = True
                assets.add(str(mo.get("asset_id", "")))
        if not we_maker and str(tr.get("trader_side", "")).upper() == "TAKER":
            assets.add(str(tr.get("asset_id", "")))
    assets.discard("")
    return assets
```

- [ ] **Step 4-6:** 测试通过 + `pytest -q` + commit（`feat(pnl): 北京日/奖励返佣归集/手续费 纯函数`）。

### Task 2.3: `realized_pnl_by_day`（FIFO 已实现盈亏按日）— 核心

**Files:** Modify `engine/pnl.py`；Test `tests/test_pnl.py`（追加）。

**Interfaces:** `realized_pnl_by_day(fills) -> {date: {"sell_profit": x, "loss": y, "fee": z}}`。输入=**单 asset** 的 `extract_fills` 结果（含 `fee_rate_bps`），FIFO 对冲，退出盈亏按 `beijing_day(卖出 ts)` 归集。调用方对每个 asset 调用并合并。

- [ ] **Step 1: 失败测试**：

```python
from engine.pnl import realized_pnl_by_day

def test_realized_pnl_fifo_profit_and_loss_by_day():
    # 买 10@0.10(day1)、买 10@0.20(day1);卖 10@0.30(day2,赚(0.30-0.10)*10=2.0)、
    # 卖 10@0.15(day3,亏(0.20-0.15)*10=0.5)。FIFO:先平最早的 0.10 lot。
    fills = [
        {"side": "BUY", "price": 0.10, "size": 10, "ts": 1735776000, "fee_rate_bps": 0},  # UTC 01-02 00:00 -> 北京 01-02
        {"side": "BUY", "price": 0.20, "size": 10, "ts": 1735776001, "fee_rate_bps": 0},
        {"side": "SELL", "price": 0.30, "size": 10, "ts": 1735862400, "fee_rate_bps": 0},  # +1 day -> 01-03
        {"side": "SELL", "price": 0.15, "size": 10, "ts": 1735948800, "fee_rate_bps": 0},  # +2 day -> 01-04
    ]
    out = realized_pnl_by_day(fills)
    assert abs(out["2026-01-03"]["sell_profit"] - 2.0) < 1e-9
    assert out["2026-01-03"].get("loss", 0) == 0
    assert abs(out["2026-01-04"]["loss"] - 0.5) < 1e-9

def test_realized_pnl_counts_taker_fee_by_sell_day():
    fills = [
        {"side": "BUY", "price": 0.10, "size": 10, "ts": 1735776000, "fee_rate_bps": 0},
        {"side": "SELL", "price": 0.30, "size": 10, "ts": 1735862400, "fee_rate_bps": 30},  # fee=0.003*... =30/1e4*0.3*10=0.009
    ]
    out = realized_pnl_by_day(fills)
    assert abs(out["2026-01-03"]["fee"] - 0.009) < 1e-9

def test_realized_pnl_unmatched_sell_ignored():
    # 卖多于买(数据滞后)——无买可对冲的部分忽略,不产生负盈亏。
    fills = [{"side": "SELL", "price": 0.30, "size": 10, "ts": 1735862400, "fee_rate_bps": 0}]
    assert realized_pnl_by_day(fills) == {} or realized_pnl_by_day(fills).get("2026-01-03", {}).get("sell_profit", 0) == 0
```

- [ ] **Step 2:** FAIL。
- [ ] **Step 3: 实现**（`engine/pnl.py` 追加）：

```python
def realized_pnl_by_day(fills) -> dict:
    """单 asset 的 extract_fills(含 fee_rate_bps)-> 按北京日的已实现盈亏。

    FIFO 回放：买入入队;卖出从最早买入 lots 对冲,对冲部分已实现盈亏
    =(卖价-买价)*量,按 beijing_day(卖出 ts) 归:正入 sell_profit、负入 loss(绝对值)。
    手续费 fee_from_fill 按该 fill 的北京日累加(买卖都算,maker=0)。无买可冲的卖量忽略。
    """
    out: dict = {}

    def _bucket(d):
        return out.setdefault(d, {"sell_profit": 0.0, "loss": 0.0, "fee": 0.0})

    ordered = sorted(fills, key=lambda f: f.get("ts", 0) or 0)
    lots: list = []  # FIFO:[{price, remaining}]
    for f in ordered:
        side = str(f.get("side", "")).upper()
        size = float(f.get("size", 0) or 0)
        price = float(f.get("price", 0) or 0)
        ts = f.get("ts", 0)
        fee = fee_from_fill(f)
        if fee:
            _bucket(beijing_day(ts))["fee"] += fee
        if size <= 0:
            continue
        if side == "BUY":
            lots.append({"price": price, "remaining": size})
        elif side == "SELL":
            qty = size
            realized = 0.0
            while qty > 1e-9 and lots:
                lot = lots[0]
                take = min(lot["remaining"], qty)
                realized += (price - lot["price"]) * take
                lot["remaining"] -= take
                qty -= take
                if lot["remaining"] <= 1e-9:
                    lots.pop(0)
            d = beijing_day(ts)
            if realized >= 0:
                _bucket(d)["sell_profit"] += realized
            else:
                _bucket(d)["loss"] += -realized
    # 清掉全 0 的空日
    return {d: v for d, v in out.items() if v["sell_profit"] or v["loss"] or v["fee"]}
```

- [ ] **Step 4-6:** 测试通过 + `pytest -q` + commit（`feat(pnl): realized_pnl_by_day FIFO 按日已实现盈亏`）。

---

## Phase 3：DB `daily_pnl`

### Task 3.1: 表 + upsert + 查询 + 汇总（`models/database.py`）

**Files:** Modify `models/database.py`（建表约 177、`record_net_worth` 附近）；Test `tests/test_database.py`（追加 `TestDailyPnl`）。

**Interfaces:**
- `upsert_daily_pnl(wallet, date, reward, rebate, sell_profit, loss, fee)`（net 内部算）。
- `get_daily_pnl(wallet, from_date, to_date) -> list[dict]`（升序）。
- `get_daily_pnl_all(from_date, to_date) -> list[dict]`（按日期跨钱包求和）。

- [ ] **Step 1: 失败测试**（`tests/test_database.py` 追加）：

```python
class TestDailyPnl:
    def test_upsert_and_get(self, db):
        db.upsert_daily_pnl("0xA", "2026-06-01", reward=7, rebate=0.5, sell_profit=2, loss=1, fee=0.1)
        row = db.get_daily_pnl("0xA", "2026-06-01", "2026-06-01")[0]
        assert row["reward"] == 7 and row["rebate"] == 0.5
        assert abs(row["net"] - (7 + 0.5 + 2 - 1 - 0.1)) < 1e-9

    def test_upsert_overwrites_same_day(self, db):
        db.upsert_daily_pnl("0xA", "2026-06-01", 1, 0, 0, 0, 0)
        db.upsert_daily_pnl("0xA", "2026-06-01", 9, 0, 0, 0, 0)  # 幂等覆盖
        rows = db.get_daily_pnl("0xA", "2026-06-01", "2026-06-01")
        assert len(rows) == 1 and rows[0]["reward"] == 9

    def test_get_all_aggregates_across_wallets(self, db):
        db.upsert_daily_pnl("0xA", "2026-06-01", reward=7, rebate=0, sell_profit=0, loss=0, fee=0)
        db.upsert_daily_pnl("0xB", "2026-06-01", reward=3, rebate=0, sell_profit=0, loss=0, fee=0)
        agg = db.get_daily_pnl_all("2026-06-01", "2026-06-01")[0]
        assert agg["date"] == "2026-06-01" and agg["reward"] == 10
```

- [ ] **Step 2:** FAIL。
- [ ] **Step 3: 建表**（`_create_tables` 内，`net_worth_history` 之后）：

```sql
            CREATE TABLE IF NOT EXISTS daily_pnl (
                wallet TEXT NOT NULL,
                date TEXT NOT NULL,
                reward REAL NOT NULL DEFAULT 0,
                rebate REAL NOT NULL DEFAULT 0,
                sell_profit REAL NOT NULL DEFAULT 0,
                loss REAL NOT NULL DEFAULT 0,
                fee REAL NOT NULL DEFAULT 0,
                net REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT (strftime('%s','now')),
                PRIMARY KEY (wallet, date)
            );
```

- [ ] **Step 4: 方法**（`record_net_worth` 之后）：

```python
    def upsert_daily_pnl(self, wallet, date, reward, rebate, sell_profit, loss, fee):
        net = reward + rebate + sell_profit - loss - fee
        c = self.conn.cursor()
        c.execute(
            "INSERT INTO daily_pnl (wallet, date, reward, rebate, sell_profit, loss, fee, net)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(wallet, date) DO UPDATE SET"
            " reward=excluded.reward, rebate=excluded.rebate, sell_profit=excluded.sell_profit,"
            " loss=excluded.loss, fee=excluded.fee, net=excluded.net,"
            " updated_at=strftime('%s','now')",
            (wallet, date, reward, rebate, sell_profit, loss, fee, net),
        )
        self.conn.commit()

    def get_daily_pnl(self, wallet, from_date, to_date) -> list[dict]:
        c = self.conn.cursor()
        c.execute(
            "SELECT date, reward, rebate, sell_profit, loss, fee, net FROM daily_pnl"
            " WHERE wallet = ? AND date >= ? AND date <= ? ORDER BY date",
            (wallet, from_date, to_date),
        )
        return [dict(r) for r in c.fetchall()]

    def get_daily_pnl_all(self, from_date, to_date) -> list[dict]:
        c = self.conn.cursor()
        c.execute(
            "SELECT date, SUM(reward) reward, SUM(rebate) rebate, SUM(sell_profit) sell_profit,"
            " SUM(loss) loss, SUM(fee) fee, SUM(net) net FROM daily_pnl"
            " WHERE date >= ? AND date <= ? GROUP BY date ORDER BY date",
            (from_date, to_date),
        )
        return [dict(r) for r in c.fetchall()]
```

- [ ] **Step 5-6:** 测试通过 + `pytest -q` + commit（`feat(db): daily_pnl 表 + upsert/查询/全钱包汇总`）。

---

## Phase 4：编排补漏 `engine/pnl_ledger.py` + wiring

### Task 4.1: `rebuild_wallet_pnl`（编排，有 IO）

**Files:** Create `engine/pnl_ledger.py`；Test `tests/test_pnl_ledger.py`（新建，mock api/db）。

**Interfaces:** `rebuild_wallet_pnl(api, db, wallet, from_date, to_date, recent_days=3)`：拉 `/activity`+`get_trades` → 按日组装 → 对 `[from_date, to_date]` 每天 upsert（含 0 行，保证近 recent_days 覆盖重算）。

- [ ] **Step 1: 失败测试** `tests/test_pnl_ledger.py`：

```python
from unittest.mock import MagicMock
from engine.pnl_ledger import rebuild_wallet_pnl

def test_rebuild_upserts_reward_and_realized():
    api = MagicMock()
    api.get_funder.return_value = "0xFUND"
    api.get_activity.return_value = [
        {"type": "REWARD", "usdcSize": 7.0, "timestamp": 1735776000},  # -> 2026-01-01
    ]
    # 一个 asset:买10@0.1(01-02)、卖10@0.3(01-03,赚2)
    api.get_trades.return_value = [
        {"id": "t1", "match_time": "1735776000", "trader_side": "MAKER", "asset_id": "A",
         "maker_orders": [{"maker_address": "0xFUND", "asset_id": "A", "side": "BUY",
                           "matched_amount": "10", "price": "0.1", "fee_rate_bps": ""}]},
        {"id": "t2", "match_time": "1735862400", "trader_side": "MAKER", "asset_id": "A",
         "maker_orders": [{"maker_address": "0xFUND", "asset_id": "A", "side": "SELL",
                           "matched_amount": "10", "price": "0.3", "fee_rate_bps": ""}]},
    ]
    db = MagicMock()
    rebuild_wallet_pnl(api, db, "0xW", "2026-01-01", "2026-01-03")
    calls = {c.kwargs.get("date") or c.args[1]: c for c in db.upsert_daily_pnl.call_args_list}
    # 每天都 upsert(含空日),共 3 天
    assert set(calls) == {"2026-01-01", "2026-01-02", "2026-01-03"}
    # 归集正确
    def _val(c, k):
        return c.kwargs.get(k)
    assert _val(calls["2026-01-01"], "reward") == 7.0
    assert _val(calls["2026-01-03"], "sell_profit") == 2.0
```

- [ ] **Step 2:** FAIL。
- [ ] **Step 3: 实现** `engine/pnl_ledger.py`：

```python
"""engine/pnl_ledger.py — 台账编排（有 IO）:拉 API -> 纯计算 -> upsert daily_pnl。"""

import logging
from datetime import datetime, timedelta
from engine.fills import extract_fills
from engine.pnl import reward_rebate_by_day, realized_pnl_by_day, our_traded_assets

logger = logging.getLogger(__name__)


def _date_range(from_date, to_date):
    d = datetime.strptime(from_date, "%Y-%m-%d")
    end = datetime.strptime(to_date, "%Y-%m-%d")
    while d <= end:
        yield d.strftime("%Y-%m-%d")
        d += timedelta(days=1)


def rebuild_wallet_pnl(api, db, wallet, from_date, to_date, recent_days=3):
    """重算 [from_date, to_date] 每天的 daily_pnl 并 upsert（幂等覆盖）。

    奖励/返佣来自公开 /activity;卖出盈亏/手续费来自 get_trades(FIFO,逐 asset 合并);
    结算盈亏 v1 先不计。每天都写(含空日),使近 recent_days 未定稿的天每轮重算刷新。
    """
    funder = api.get_funder()
    activity = api.get_activity(types=["REWARD", "MAKER_REBATE", "REDEEM"])
    rr = reward_rebate_by_day(activity)

    trades = api.get_trades_for_pnl() if hasattr(api, "get_trades_for_pnl") else _all_trades(api, funder)
    realized: dict = {}
    for asset in our_traded_assets(trades, funder):
        for d, v in realized_pnl_by_day(extract_fills(trades, funder, asset)).items():
            agg = realized.setdefault(d, {"sell_profit": 0.0, "loss": 0.0, "fee": 0.0})
            agg["sell_profit"] += v["sell_profit"]
            agg["loss"] += v["loss"]
            agg["fee"] += v["fee"]

    for d in _date_range(from_date, to_date):
        r = rr.get(d, {})
        z = realized.get(d, {})
        db.upsert_daily_pnl(
            wallet=wallet, date=d,
            reward=r.get("reward", 0.0), rebate=r.get("rebate", 0.0),
            sell_profit=z.get("sell_profit", 0.0), loss=z.get("loss", 0.0), fee=z.get("fee", 0.0),
        )


def _all_trades(api, funder):
    from py_clob_client_v2.clob_types import TradeParams
    return api.get_trades(TradeParams(maker_address=funder))
```

（注：`get_trades` 用 `maker_address=funder` 全量拉，项目已自动翻页；不用 asset_id 过滤。）

- [ ] **Step 4-6:** 测试通过 + `pytest -q` + commit（`feat(pnl): rebuild_wallet_pnl 编排补漏`）。

### Task 4.2: 接入引擎（启动补漏 + 每日更新）

**Files:** Modify `engine/manager.py`（`_maybe_snapshot_networth` 附近 + 启动路径）；Test `tests/test_manager.py` 或 `test_networth_worker.py`（追加）。

**Interfaces:** WalletWorker 每日跨北京日时 `rebuild_wallet_pnl(api, db, wallet, from=近{recent}天, to=今天)`;启动补漏 `rebuild_wallet_pnl(from=max(2026-06-01, 已有最早缺口), to=昨天)`。

- [ ] **Step 1: 失败测试**（`test_networth_worker.py` 追加）：验证跨北京日时调用 `rebuild_wallet_pnl`（patch it），同日不重复；启动时以 `2026-06-01` 为下界调用。（用 monkeypatch/patch `engine.manager.rebuild_wallet_pnl`。）

```python
def test_pnl_rebuild_runs_once_per_bj_day(monkeypatch):
    # 仿 _maybe_snapshot_networth 的跨天节流:同一北京日只重算一次。
    # (按 manager 实际结构写:patch rebuild_wallet_pnl,连调两次同日只 1 次、跨天再 1 次。)
    ...
```

- [ ] **Step 2:** FAIL。
- [ ] **Step 3: 实现**：manager 加 `_last_pnl_date`；新方法 `_maybe_rebuild_pnl()`（仿 `_maybe_snapshot_networth`：北京今日 `beijing_day(time.time())`，与 `_last_pnl_date` 不同则 `rebuild_wallet_pnl(self.api, self.db, self.wallet_address, from=_prev_n(today, recent), to=today)`，成功后置日期；失败 WARNING 不阻断）。在 `_tick` 里 `_maybe_snapshot_networth()` 旁调用。启动补漏：`start_all`/worker 初始化时跑一次 `rebuild_wallet_pnl(from="2026-06-01", to=昨天)`（后台/首 tick，失败重试）。`from engine.pnl import beijing_day`。
- [ ] **Step 4-6:** 测试通过 + `pytest -q` + commit（`feat(pnl): 引擎接入台账补漏(启动6/1+每日)`）。

---

## Phase 5：路由 + 可视化

### Task 5.1: `/api/pnl` 路由（`web/routes.py`）

**Files:** Modify `web/routes.py`；Test `tests/test_pnl_route.py`（新建）。

**Interfaces:** `GET /api/pnl?wallet=<addr|all>&days=90`：返回 `{series: [{date,reward,rebate,sell_profit,loss,fee,net}], totals: {reward,...,net}, cumulative_net: [...]}`。`all` 用 `get_daily_pnl_all`,否则 `get_daily_pnl`。

- [ ] **Step 1: 失败测试** `tests/test_pnl_route.py`（仿 `test_wallet_remark_routes.py` 的 client 夹具）：

```python
def test_pnl_single_wallet(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    db.upsert_daily_pnl("0xA", "2026-06-01", reward=7, rebate=0, sell_profit=2, loss=1, fee=0)
    r = client.get("/api/pnl?wallet=0xA&days=365").get_json()
    assert r["series"][0]["reward"] == 7
    assert abs(r["totals"]["net"] - 8) < 1e-9

def test_pnl_all_aggregates(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    db.upsert_daily_pnl("0xA", "2026-06-01", 7, 0, 0, 0, 0)
    db.upsert_daily_pnl("0xB", "2026-06-01", 3, 0, 0, 0, 0)
    r = client.get("/api/pnl?wallet=all&days=365").get_json()
    assert r["series"][0]["reward"] == 10
```

- [ ] **Step 2:** FAIL。
- [ ] **Step 3: 实现**（`web/routes.py`,`@login_required`）：按 `days` 算 `from_date`（北京今日回溯，用 `engine.pnl.beijing_day(time.time())` 起）/`to_date`;`all` → `db.get_daily_pnl_all`,否则 `db.get_daily_pnl`;算 `totals`（各列 sum）与 `cumulative_net`（逐日累加）。
- [ ] **Step 4-6:** 测试通过 + `pytest -q` + commit（`feat(api): /api/pnl 每日盈亏 + 全钱包汇总`）。

### Task 5.2: 前端可视化（主会话手改）

**Files:** Modify `web/templates/networth.html`（加盈亏区）或新增 `pnl` 页 + 侧栏入口；`web/routes.py` 加页面路由（若新页）。

- [ ] **Step 1:** 钱包下拉加「全部」选项;拉 `/api/pnl`;渲染:①每日盈亏表（日期/奖励/返佣/卖出盈利/亏损/手续费/净利润，各列单独）②累计净利润折线（复用净值页 SVG 折线手法）。文案简体中文,数值 `toFixed(2)`,金额进 `escapeHtml` 非必需（数字）但钱包标签用 `walletLabel`。
- [ ] **Step 2: 校验**：`node -e` 读模板、无 BOM、`node --check`（若改 app.js）。
- [ ] **Step 3: 渲染走查**（Flask 测试客户端 GET 页面 200 + 关键标记；真数据交互由用户走查）。
- [ ] **Step 4: 提交**（`feat(ui): 每日盈亏台账可视化`）。

---

## Self-Review

**Spec coverage**：奖励(REWARD)/返佣(MAKER_REBATE)→2.2/4.1✓;卖出盈亏+手续费(get_trades FIFO)→2.1/2.3/4.1✓;结算 v1 不计→已注✓;北京日+奖励-1→2.2✓;补漏6/1+幂等→4.1/4.2✓;全钱包汇总→3.1/5.1✓;可视化→5.2✓;持久化=现有 sqlite✓;净值不动✓;禁 avgPrice/curPrice(只用 get_trades)✓。
**Placeholder**：Task 4.2 Step 1 测试骨架标了 `...`——实现时按 manager 实际结构补全（跨天节流仿 `_maybe_snapshot_networth`）；其余均完整代码。
**Type consistency**：`upsert_daily_pnl(wallet,date,reward,rebate,sell_profit,loss,fee)` 定义(3.1)与调用(4.1)一致;`realized_pnl_by_day` 返回 `{sell_profit,loss,fee}` 与 4.1 合并键一致;`reward_rebate_by_day` 返回 `{reward,rebate}` 与 4.1 一致;`beijing_day`/`fee_from_fill` 跨 task 一致。
