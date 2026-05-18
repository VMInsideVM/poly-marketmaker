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
- `engine/scanner.py` filters reward markets (reward ≥ threshold, settlement days, price band, spread, cooldown) and computes an order price via `engine/strategy.py`.
- `engine/strategy.py` (`determine_order_price`) is pure, fully unit-tested, and the core IP. It picks a buy price from orderbook bid depth using three different rules depending on tick size and `rewards_max_spread`. Change behavior here only with corresponding test updates.
- `engine/monitor.py` is API-driven: Step 1 detects fills via CLOB `get_trades` (flattens `maker_orders` by funder, deduplicates by `(trade_id, order_id)` against a per-monitor trade-history watermark), places take-profit sells, and cancels the filled buy's remaining quantity. Step 2 stop-loss uses the Polymarket Data API position `avgPrice`/`curPrice`. Step 3 re-runs `determine_order_price` on resting buys and replaces/cancels any whose price no longer matches the current tick.

**Polymarket access (`api/polymarket_api.py`)** wraps `py-clob-client-v2`. Each `PolymarketAPI` is one wallet. Default `signature_type=3` (POLY_1271 / deposit wallet), which **requires** a `funder` address (the user's Deposit Wallet from polymarket.com/settings) — adding a wallet without it fails. Rewards endpoints are static methods (no auth needed); order placement needs L2 creds derived at construction.

## Critical behaviors to preserve

- **Stopping the engine stops stop-loss monitoring.** `stop_all`/`stop_wallet` cancel open buy orders but also kill the monitor thread, leaving existing positions unprotected — the user-facing messages already warn about this; keep the warnings.
- **Startup recovery** (`startup_recovery`) seeds each monitor's trade watermark from DB trade history; offline fills are caught on the next monitor tick via `get_trades(after=watermark)` + `(trade_id, order_id)` dedup. It no longer cancels stale orders or reconciles DB positions.
- **Balance is re-read before every order placement** (not cached per batch) so concurrent fills don't cause overspend — see commit history; preserve this.
- **The `orders`/`positions` SQLite tables are no longer the source of truth.** Order management, `/api/positions`, dashboard counts, and the monitor all read the live Polymarket API; the DB keeps only `trades` (history) and `cooldown` (plus wallet/settings/password config).
- Orders are placed lowest-`market_competitiveness` first (less competition = larger reward share).
- Default strategy/risk parameters live in `config.py` `DEFAULTS`; user overrides are stored per-key in the `settings` table and merged on read. Settings changes take effect on next engine start unless restarted.

## Reference

Design rationale and the canonical filtering flow are in `docs/superpowers/specs/2026-05-17-polymarket-market-maker-design.md` (in 简体中文). The scanner intentionally mirrors the flow documented at the top of `test_live.py`.
