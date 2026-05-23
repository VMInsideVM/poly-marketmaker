# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A local single-user Flask web app that automates reward-farming market making on Polymarket. The end user is non-technical: they double-click an executable, a browser opens, they enter wallet private keys, and the app places/monitors orders to earn liquidity rewards. UI strings and user-facing messages are in 简体中文 — keep them that way. Intended to be packaged as a single exe via PyInstaller.

## Commands

```powershell
pip install -r requirements.txt   # also need pytest for the test suite
python app.py                     # run app; opens browser at http://127.0.0.1:5000 (/setup on first run)
pytest                            # run unit tests
pytest tests/test_strategy.py     # single file
pytest tests/test_strategy.py::TestMaxSpread2_TickSize1Cent::test_bid1_gt_2000_place_at_bid2  # single test
```

`tests/` holds pytest unit tests (pure logic, no network). The top-level `test_live.py`, `test_simulate.py`, `test_real_order.py` are **not** pytest tests — they are manual scripts that hit the real Polymarket API and the local `market_maker.db`; `test_real_order.py` places actual orders. Do not run them as part of a test suite.

## Architecture

**Auth gates everything.** Wallet private keys are AES-256-GCM encrypted with a key derived (PBKDF2, 600k iterations) from the user's password (`utils/crypto.py`). The key exists only in memory after login, so engines **cannot auto-start on boot** — the user must log in first. Note `app.py` imports but never calls `init_manager`; the `EngineManager` is constructed inside the `/setup` and `/login` routes (`web/routes.py`) once the encryption key is available. `web/routes.py` holds module-global `db`, `manager`, `encryption_key` — this is a deliberately single-process, single-user design.

**Threading model (`engine/manager.py`).** One shared `MarketScanner` thread produces an eligible-markets list using a single wallet's API (scanning needs no wallet-specific auth). Each enabled wallet gets a `WalletWorker` with its own thread that (a) places orders from the shared list and (b) monitors fills/stop-loss. The SQLite connection is opened once with `check_same_thread=False` and shared across all threads (`models/database.py`).

**Two operating modes** exposed via `/api/engine/*`:
- *Auto*: `start_all()` runs recovery + wallet workers + a periodic scanner loop.
- *Manual*: separate `scan` / `place-orders` / `start-monitors` endpoints so the user drives each step. Manual scan results are persisted to the `eligible_markets` table and surfaced live via `/api/eligible` (memory while scanning, DB when idle).

**Pipeline: scan → strategy → place → monitor.**
- `engine/scanner.py` filters reward markets (reward ≥ threshold, settlement days, price band, spread, cooldown) and computes an order price via `engine/strategy.py`. It does **not** filter by balance (the eligible list is shared across wallets with different balances); instead it records a per-market `min_cost = rewards_min_size × ceil_to_tick(reward_range_min)` (the minimum capital to place a reward-qualifying order) that each wallet gates on at placement time.
- `engine/strategy.py` (`determine_order_price`) is pure, fully unit-tested, and the core IP. It picks a buy price from orderbook bid depth using three different rules depending on tick size and `rewards_max_spread`. Change behavior here only with corresponding test updates.
- `engine/monitor.py` is API-driven, run as one tick (`WalletWorker._tick`). Step 1 (`check_buy_orders`) detects fills via CLOB `get_trades` (flattens `maker_orders` by funder, deduplicates by `(trade_id, order_id)` against a per-monitor trade-history watermark), records the buy for history, sets cooldown, and cancels the filled buy's remaining quantity — it no longer places the sell. Step 1b (`check_take_profit`) is **position-driven**: it maintains **exactly one** resting SELL per position; the cost basis is the weighted average of our real CLOB `get_trades` buy fills (`cost_basis_from_buy_fills` in `engine/take_profit.py`) — NOT the Data API `avgPrice` (which was observed to glitch on freshly opened positions, causing undersell); the Data API position is used only for `size`. The sell price is `max(ceil_to_tick(cost), best_bid + tick)` (穿价护栏: never sell below cost, always rest as a maker, never cross the book), cancelling/replacing any sells that don't match. This replaced the old per-fill sell, which split one position into many orders and priced sells off per-fill `maker_orders` data that diverged from the real average cost. Step 2 stop-loss uses the Data API position `avgPrice`/`curPrice`. Step 3 first re-checks the **bid-ask spread** on each resting buy (`engine/eligibility.py` `recheck_resting_buy`, computed from the live orderbook): if `(best_ask − best_bid)` is at/over `max_spread_cents` it cancels the buy (cancel-only, no cooldown, positions untouched); an undeterminable spread (book side missing) is kept. Surviving buys then go through `determine_order_price` and are replaced/cancelled if their price no longer matches the current tick.

**Polymarket access (`api/polymarket_api.py`)** wraps `py-clob-client-v2`. Each `PolymarketAPI` is one wallet. Default `signature_type=2` (POLY_GNOSIS_SAFE), which needs a `funder` address — the user's deposit wallet (the Polymarket Gnosis Safe that actually holds funds), **not** the signer EOA. The user only enters a private key: when `funder` is omitted, `PolymarketAPI` derives it from the signer EOA via `derive_deposit_address` (CREATE2 against the Polymarket Safe factory, using the official `py_builder_relayer_client`), and `api_add_wallet` stores that derived address in the `wallets.funder` column. This derivation is verified to reproduce real deposit addresses. Rewards endpoints are static methods (no auth needed); order placement needs L2 creds derived at construction.

## Critical behaviors to preserve

- **Stopping the engine stops stop-loss monitoring.** `stop_all`/`stop_wallet` cancel open buy orders but also kill the monitor thread, leaving existing positions unprotected — the user-facing messages already warn about this; keep the warnings.
- **Startup recovery** (`startup_recovery`) seeds each monitor's trade watermark from DB trade history; offline fills are caught on the next monitor tick via `get_trades(after=watermark)` + `(trade_id, order_id)` dedup. It no longer cancels stale orders or reconciles DB positions.
- **Balance is re-read before every order placement** (not cached per batch) so concurrent fills don't cause overspend — see commit history; preserve this. `place_orders` also reads each wallet's balance up front and skips any market whose recorded `min_cost` exceeds it, before fetching the orderbook.
- **The `orders`/`positions` SQLite tables are no longer the source of truth.** Order management, `/api/positions`, dashboard counts, and the monitor all read the live Polymarket API. The `trades` table only holds `stop_loss` records now — the monitor no longer writes buy/sell rows (per-fill `maker_orders` prices were unreliable). Buy "history" in the UI **is the live Data API position** (`avgPrice`/`size`): the history page's holdings table reads `/api/positions`, and sell/stop-loss actions are shown from the `actions` table ("卖单理由"). The monitor watermark (`init_watermark`) seeds from `max(created_at)` over both `trades` and `actions` so clearing `trades` does not reset it.
- Orders are placed lowest-`market_competitiveness` first (less competition = larger reward share).
- Default strategy/risk parameters live in `config.py` `DEFAULTS`; user overrides are stored per-key in the `settings` table and merged on read. Settings changes take effect on next engine start unless restarted.

## Reference

Design rationale and the canonical filtering flow are in `docs/superpowers/specs/2026-05-17-polymarket-market-maker-design.md` (in 简体中文). The scanner intentionally mirrors the flow documented at the top of `test_live.py`.
