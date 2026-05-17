"""test_live.py — 按正确流程筛选5个符合条件的市场。

筛选流程:
1. /rewards/markets/multi — 粗筛 rate_per_day > 100 的市场
2. Gamma API GET /markets/{id} — 确认结算日期 > 4 天
3. /rewards/markets/{condition_id} — 获取每个 token 独立的 rate_per_day，
   确认价格 10~50 美分的 token 奖励 > 100
4. GET /book — 检查订单簿价差 < 3 美分，余额足够
"""

import time
from datetime import datetime
from models.database import Database
from utils.crypto import decrypt, derive_key
from config import DB_PATH
import hashlib


def parse_end_date(s):
    if not s:
        return 0
    # 去掉末尾的时区偏移 (+00, +00:00, Z 等)
    import re

    # 匹配时间戳后的时区: +00, +00:00, +0000, Z
    # 但不能误匹配日期中的 -01 (月/日)
    s_clean = s.strip()
    if s_clean.endswith("Z"):
        s_clean = s_clean[:-1]
    else:
        # 只有在时间部分（含:）之后才去掉时区
        m = re.search(r"(\d{2}:\d{2}:\d{2})[+-]\d{2}:?\d{0,2}$", s_clean)
        if m:
            s_clean = s_clean[: m.end(1)]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s_clean, fmt).timestamp()
        except ValueError:
            continue
    return 0


def safe_call(fn, default=None, retries=3):
    for i in range(retries):
        try:
            return fn()
        except Exception as e:
            if i < retries - 1:
                time.sleep(1)
            else:
                print(f"    ⚠ API 失败: {e}")
                return default


def display_market(idx, c):
    print(f"\n{'=' * 70}")
    print(f"#{idx}  {c['question']}")
    print(f"{'=' * 70}")
    print(f"  网址:          {c['url']}")
    print(f"  方向:          {c['outcome']} (token价格: {c['token_price']:.2f})")
    print(f"  condition_id:  {c['condition_id']}")
    print()
    print(f"  【奖励】")
    print(f"    市场每日奖励:    ${c['market_daily_rate']:.2f}/天")
    print(f"    max_spread:      {c['max_spread']}")
    print(f"    min_size:        {c['min_size']}")
    print()
    print(f"  【市场】")
    print(f"    结算日期: {c['end_date']} (剩余 {c['days_left']:.0f} 天)")
    print(f"    tick_size: {c['tick_size']}")
    print(f"    买卖价差: {c['spread_cents']:.2f} 美分")
    print(f"    所需资金: {c['required']:.2f} pUSD")
    print()
    print(f"  【订单簿】")
    print(f"    ┌{'─' * 28}┬{'─' * 28}┐")
    print(f"    │ {'买盘 (Bids)':^26} │ {'卖盘 (Asks)':^26} │")
    print(f"    │ {'价格':>10}  {'数量':<12} │ {'价格':>10}  {'数量':<12} │")
    print(f"    ├{'─' * 28}┼{'─' * 28}┤")
    max_rows = max(len(c["bids"]), len(c["asks"]))
    for r in range(max_rows):
        bid_str = (
            f" {c['bids'][r]['price']:>10}  {c['bids'][r]['size']:<12}"
            if r < len(c["bids"])
            else " " * 26
        )
        ask_str = (
            f" {c['asks'][r]['price']:>10}  {c['asks'][r]['size']:<12}"
            if r < len(c["asks"])
            else " " * 26
        )
        print(f"    │ {bid_str:26} │ {ask_str:26} │")
    print(f"    └{'─' * 28}┴{'─' * 28}┘")


def main():
    print("=" * 70)
    print("Polymarket 做市助手 — 精确筛选符合条件的市场")
    print("=" * 70)

    db = Database(DB_PATH)
    db.init()
    pw_hash, salt = db.get_password()
    if pw_hash is None:
        print("✗ 未设置密码")
        return
    password = input("请输入访问密码: ")
    key = derive_key(password, salt)
    if hashlib.sha256(key).hexdigest() != pw_hash:
        print("✗ 密码错误")
        return

    wallets = db.list_wallets()
    if not wallets:
        print("✗ 没有钱包")
        return

    w = wallets[0]
    private_key = decrypt(w["encrypted_key"], key)

    from api.polymarket_api import PolymarketAPI

    api = PolymarketAPI(private_key)

    balance = safe_call(api.get_balance, 0)
    print(f"\n钱包: {api.get_address()}")
    print(f"余额: {balance:.2f} pUSD")

    settings = db.get_settings()
    min_reward = settings["min_reward_usd"]
    max_spread_cents = settings["max_spread_cents"]
    min_price_cents = settings["min_price_cents"]
    max_price_cents = settings["max_price_cents"]
    min_days = settings["min_settlement_days"]

    print(f"\n{'─' * 70}")
    print(f"筛选条件:")
    print(f"  每日奖励（单token）≥ ${min_reward}/天")
    print(f"  买卖价差 < {max_spread_cents} 美分")
    print(f"  单价范围 {min_price_cents}~{max_price_cents} 美分")
    print(f"  结算日期 > {min_days} 天")
    print(f"{'─' * 70}")

    # ============================================================
    # Step 1: 获取奖励市场列表（粗筛：总 rate_per_day 排序）
    # ============================================================
    print(f"\n[Step 1] 获取奖励市场（按 rate_per_day 降序）...")
    markets = safe_call(
        lambda: api.get_rewards_markets(
            min_price=0.10,
            max_price=0.90,
        ),
        [],
    )
    if not markets:
        print("  ✗ 未获取到市场，请检查网络")
        return
    print(f"  获取到 {len(markets)} 个市场")

    # 粗筛：总 rate_per_day >= min_reward
    candidates = []
    for m in markets:
        rc = m.get("rewards_config", [])
        total_rate = sum(r.get("rate_per_day", 0) for r in rc)
        if total_rate >= min_reward:
            candidates.append(m)
    print(f"  粗筛（总奖励≥${min_reward}）后剩余: {len(candidates)} 个")

    # ============================================================
    # Step 2 & 3 & 4: 逐个精确检查
    # ============================================================
    matched = []
    stats = {
        "date": 0,
        "token_reward": 0,
        "price": 0,
        "spread": 0,
        "balance": 0,
        "no_book": 0,
    }

    for i, market in enumerate(candidates):
        condition_id = market.get("condition_id", "")
        question = market.get("question", "N/A")
        market_slug = market.get("market_slug", "")
        event_slug = market.get("event_slug", "")
        market_id = market.get("market_id", "")
        market_competitiveness = market.get("market_competitiveness", "N/A")
        tokens = market.get("tokens", [])
        max_spread = market.get("rewards_max_spread", 0)
        min_size = int(market.get("rewards_min_size", 0))
        total_rate = sum(
            r.get("rate_per_day", 0) for r in market.get("rewards_config", [])
        )

        if not tokens:
            continue

        print(f"\n  [{i+1}/{len(candidates)}] 检查: {question}")

        # --- Step 2: 结算日期 ---
        end_date_str = market.get("end_date", "")
        end_ts = parse_end_date(end_date_str)
        days_left = (end_ts - time.time()) / 86400 if end_ts else 0

        # 只排除 0~4 天之间的市场，负数（解析失败或无限期）算通过
        if 0 <= days_left < min_days:
            print(
                f"    ✗ 结算日期太近 ({days_left:.0f}天，在0~{min_days}天之间) end_date原始值: '{end_date_str}'"
            )
            stats["date"] += 1
            continue

        # --- Step 3: 获取 per-token rewards ---
        raw_rewards = safe_call(
            lambda cid=condition_id: api.get_rewards_for_market(cid), []
        )
        time.sleep(0.3)

        # 展示 raw_rewards 的完整 rewards_config
        print(f"    [raw rewards] 返回 {len(raw_rewards)} 条数据:")
        total_from_raw = 0
        per_config_rates = []
        for rd in raw_rewards:
            rd_cid = rd.get("condition_id", "?")
            rd_question = rd.get("question", "")
            if rd_question:
                print(f"      condition_id={rd_cid}")
                print(f"      question={rd_question}")
            rd_config = rd.get("rewards_config", [])
            for rc in rd_config:
                rc_id = rc.get("id", "?")
                rate = rc.get("rate_per_day", 0)
                asset = rc.get("asset_address", "?")
                start = rc.get("start_date", "?")
                end = rc.get("end_date", "?")
                print(
                    f"      id={rc_id}, rate_per_day=${rate}, asset={asset}, period={start}~{end}"
                )
                total_from_raw += rate
                per_config_rates.append(rate)

        if not per_config_rates:
            print(f"      (无数据，使用 /multi 的总和)")
            total_from_raw = total_rate

        # 该 market 的总每日奖励 = rewards_config 中所有 rate_per_day 之和
        market_reward = total_from_raw
        print(f"    该market总 rate_per_day=${market_reward:.2f}")

        # 判断该 market 的奖励是否 >= 阈值
        if market_reward < min_reward:
            print(f"    ✗ market奖励不足 (${market_reward:.2f} < ${min_reward})")
            stats["token_reward"] += 1
            continue

        # --- Step 3b: 检查该 market 是否有 token 价格在 10~50 美分之间 ---
        # 展示所有 token 的价格
        print(f"    tokens:")
        for t in tokens:
            t_price = float(t.get("price", 0))
            t_outcome = t.get("outcome", "?")
            in_range = min_price_cents <= t_price * 100 <= max_price_cents
            mark = "✓" if in_range else "✗"
            print(
                f"      {mark} {t_outcome}: {t_price * 100:.1f}美分 {'(符合)' if in_range else '(超范围)'}"
            )

        # 找到价格在范围内的 token
        valid_tokens = [
            t
            for t in tokens
            if min_price_cents <= float(t.get("price", 0)) * 100 <= max_price_cents
        ]
        if not valid_tokens:
            print(
                f"    ✗ 该market没有价格在 [{min_price_cents}, {max_price_cents}] 美分范围内的token"
            )
            stats["price"] += 1
            continue

        # 对每个符合价格的 token 检查订单簿（只要一个符合就行）
        for token in valid_tokens:
            token_id = token.get("token_id", "")
            token_price = float(token.get("price", 0))
            outcome = token.get("outcome", "?")
            print(f"    检查 {outcome} token_id={token_id}")

            # --- Step 4a: 用 GET /spread 快速筛选价差 ---
            spread_val = safe_call(lambda tid=token_id: api.get_spread(tid), -1)
            time.sleep(0.2)

            if spread_val is None or spread_val < 0:
                print(f"    ✗ {outcome} 无订单簿")
                stats["no_book"] += 1
                continue

            spread_cents = spread_val * 100
            print(f"    {outcome} spread={spread_cents:.2f}美分 (via GET /spread)")

            if spread_cents >= max_spread_cents:
                print(
                    f"    ✗ {outcome} 价差过大 ({spread_cents:.2f}美分 >= {max_spread_cents}美分)"
                )
                stats["spread"] += 1
                continue

            # --- Step 4b: 价差通过，拉完整订单簿 ---
            ob = safe_call(lambda tid=token_id: api.get_orderbook(tid), {})
            time.sleep(0.2)

            if not ob or not ob.get("bids") or not ob.get("asks"):
                stats["no_book"] += 1
                continue

            bids = sorted(
                ob["bids"], key=lambda x: float(x["price"]), reverse=True
            )  # 降序，买一在前
            asks = sorted(ob["asks"], key=lambda x: float(x["price"]))  # 升序，卖一在前
            tick_size = ob.get("tick_size", "0.01")
            best_bid = float(bids[0]["price"])  # 买一（最高买价）
            best_ask = float(asks[0]["price"])  # 卖一（最低卖价）

            print(
                f"    {outcome} 买一={bids[0]}, 卖一={asks[0]}, 价差={( best_ask - best_bid) * 100:.2f}美分"
            )

            # 确认价格（用实际 best_bid）
            if best_bid * 100 < min_price_cents or best_bid * 100 > max_price_cents:
                stats["price"] += 1
                continue

            required = min_size * best_bid

            # URL
            if market_slug:
                url = f"https://polymarket.com/market/{market_slug}"
            elif event_slug:
                url = f"https://polymarket.com/event/{event_slug}"
            else:
                url = f"https://polymarket.com"

            print(f"    ✓ {outcome} 符合所有条件！")
            matched.append(
                {
                    "question": question,
                    "outcome": outcome,
                    "condition_id": condition_id,
                    "token_id": token_id,
                    "market_competitiveness": market_competitiveness,
                    "token_price": token_price,
                    "url": url,
                    "market_daily_rate": market_reward,
                    "max_spread": max_spread,
                    "min_size": min_size,
                    "days_left": days_left,
                    "end_date": end_date_str,
                    "tick_size": tick_size,
                    "spread_cents": spread_cents,
                    "required": required,
                    "bids": [
                        {"price": b["price"], "size": b["size"]} for b in bids[:5]
                    ],
                    "asks": [
                        {"price": a["price"], "size": a["size"]} for a in asks[:5]
                    ],
                }
            )
            break  # 该 market 已找到一个符合的 token，不再检查其他 token

    # ============================================================
    # 展示结果
    # ============================================================
    print(f"\n{'═' * 70}")
    print(f"筛选完成: 找到 {len(matched)} 个符合条件的市场")
    print(
        f"跳过统计: 结算太近={stats['date']}, token奖励不足={stats['token_reward']}, "
        f"价格超范围={stats['price']}, 价差过大={stats['spread']}, "
        f"余额不足={stats['balance']}, 无订单簿={stats['no_book']}"
    )
    print(f"{'═' * 70}")

    if matched:
        # 按 market_competitiveness 降序排序
        matched.sort(
            key=lambda x: float(x.get("market_competitiveness", 0) or 0), reverse=True
        )

        # 汇总表格
        print(f"\n{'─' * 120}")
        print(
            f"{'#':<4} {'Market Name':<40} {'Condition ID':<68} {'Token ID':<80} {'Competitiveness':<16}"
        )
        print(f"{'─' * 120}")
        for idx, m in enumerate(matched, 1):
            print(
                f"{idx:<4} {m['question']:<40} {m['condition_id']:<68} {m['token_id']:<80} {m['market_competitiveness']}"
            )
        print(f"{'─' * 120}")

        # 详细展示
        for idx, m in enumerate(matched, 1):
            display_market(idx, m)
    else:
        print("\n没有找到符合条件的市场。建议:")
        print("  - 降低最低奖励金额")
        print("  - 扩大价格范围")
        print("  - 放宽价差条件")

    db.close()


if __name__ == "__main__":
    main()
