"""test_live.py — 用 Gamma API + CLOB API 组合筛选，找出5个符合条件的市场。"""

import time
from datetime import datetime, timedelta
from models.database import Database
from utils.crypto import decrypt, derive_key
from config import DB_PATH
import hashlib


def safe_call(fn, default=None, retries=3):
    """带重试的 API 调用。"""
    for i in range(retries):
        try:
            return fn()
        except Exception as e:
            if i < retries - 1:
                time.sleep(1)
            else:
                print(f"    ⚠ API 调用失败: {e}")
                return default


def display_market(idx, c):
    """展示一个符合条件的市场。"""
    print(f"\n{'=' * 70}")
    print(f"#{idx}  {c['question']}")
    print(f"{'=' * 70}")
    print(f"  网址:        {c['url']}")
    print(f"  方向:        {c['outcome']}")
    print()
    print(f"  【奖励参数】")
    print(f"    每日奖励:     ${c['daily_rate']:.2f}/天")
    print(f"    max_spread:   {c['max_spread']}")
    print(f"    min_size:     {c['min_size']}")
    print()
    print(f"  【市场参数】")
    print(f"    结算日期:     {c['end_date']} (剩余 {c['days_left']:.0f} 天)")
    print(f"    tick_size:    {c['tick_size']}")
    print(f"    买卖价差:     {c['spread_cents']:.2f} 美分")
    print(f"    所需资金:     {c['required']:.2f} pUSD")
    print()
    print(f"  【订单簿】")
    print(f"    ┌{'─' * 28}┬{'─' * 28}┐")
    print(f"    │ {'买盘 (Bids)':^26} │ {'卖盘 (Asks)':^26} │")
    print(f"    │ {'价格':>10}  {'数量':<12} │ {'价格':>10}  {'数量':<12} │")
    print(f"    ├{'─' * 28}┼{'─' * 28}┤")
    max_rows = max(len(c["bids"]), len(c["asks"]))
    for r in range(max_rows):
        if r < len(c["bids"]):
            b = c["bids"][r]
            bid_str = f" {b['price']:>10}  {b['size']:<12}"
        else:
            bid_str = " " * 26
        if r < len(c["asks"]):
            a = c["asks"][r]
            ask_str = f" {a['price']:>10}  {a['size']:<12}"
        else:
            ask_str = " " * 26
        print(f"    │ {bid_str:26} │ {ask_str:26} │")
    print(f"    └{'─' * 28}┴{'─' * 28}┘")
    print()
    print(f"  【筛选结果】")
    for mark, desc in c["checks"]:
        print(f"    {mark} {desc}")
    print(f"    → 全部通过 ✓")


def main():
    print("=" * 70)
    print("Polymarket 做市助手 — 市场筛选（Gamma + CLOB 组合）")
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
    print(f"\n{'─' * 70}")
    print("筛选条件:")
    print(f"  每日奖励  ≥ ${settings['min_reward_usd']}/天")
    print(f"  买卖价差  < {settings['max_spread_cents']} 美分")
    print(
        f"  单价范围  {settings['min_price_cents']}~{settings['max_price_cents']} 美分"
    )
    print(f"  结算日期  > {settings['min_settlement_days']} 天")
    print(f"{'─' * 70}")

    # ===== Step 1: Gamma API 精确筛选 =====
    min_end_date = (
        datetime.now() + timedelta(days=settings["min_settlement_days"])
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(
        f"\n[Step 1] Gamma API 筛选 (end_date > {min_end_date[:10]}, closed=false)..."
    )
    gamma_markets = safe_call(
        lambda: api.list_markets(
            end_date_min=min_end_date,
            closed=False,
            limit=200,
        ),
        [],
    )
    print(f"  Gamma 返回 {len(gamma_markets)} 个市场")

    if not gamma_markets:
        print("✗ Gamma API 未返回数据")
        return

    # 构建 condition_id -> Gamma 市场数据 的映射
    gamma_by_condition = {}
    for gm in gamma_markets:
        cid = gm.get("conditionId", "")
        if cid:
            gamma_by_condition[cid] = gm

    # ===== Step 2: CLOB API 获取奖励数据 =====
    print(
        f"\n[Step 2] CLOB API 获取奖励市场 (按 rate_per_day 降序, 价格 {settings['min_price_cents']/100}~{settings['max_price_cents']/100})..."
    )
    clob_markets = safe_call(
        lambda: api.get_rewards_markets(
            min_price=settings["min_price_cents"] / 100,
            max_price=settings["max_price_cents"] / 100,
            max_spread=settings["max_spread_cents"] / 100,
            max_pages=3,
        ),
        [],
    )
    print(f"  CLOB 返回 {len(clob_markets)} 个奖励市场")

    if not clob_markets:
        print("✗ CLOB API 未返回数据")
        return

    # ===== Step 3: 交叉匹配 + 订单簿检查 =====
    print(f"\n[Step 3] 交叉匹配并检查订单簿...\n")

    matched = []
    checked = 0
    skip_reasons = {
        "no_gamma": 0,
        "reward": 0,
        "price": 0,
        "balance": 0,
        "spread": 0,
        "no_book": 0,
    }

    for market in clob_markets:
        if len(matched) >= 5:
            break

        condition_id = market.get("condition_id", "")
        question = market.get("question", "N/A")
        market_slug = market.get("market_slug", "")
        end_date_str = market.get("end_date", "")
        rewards_config = market.get("rewards_config", [])
        daily_rate = sum(rc.get("rate_per_day", 0) for rc in rewards_config)
        max_spread = market.get("rewards_max_spread", 0)
        min_size = int(market.get("rewards_min_size", 0))
        tokens = market.get("tokens", [])

        # 交叉验证：必须在 Gamma 结果中（即满足 end_date > min_days）
        if condition_id not in gamma_by_condition:
            skip_reasons["no_gamma"] += 1
            continue

        # 奖励筛选
        if daily_rate < settings["min_reward_usd"]:
            skip_reasons["reward"] += 1
            continue

        gamma_data = gamma_by_condition[condition_id]
        gamma_slug = gamma_data.get("slug", "")

        if not tokens:
            continue

        for token in tokens:
            if len(matched) >= 5:
                break

            token_id = token.get("token_id", "")
            token_price = float(token.get("price", 0))
            outcome = token.get("outcome", "?")

            # 价格范围
            if (
                token_price * 100 < settings["min_price_cents"]
                or token_price * 100 > settings["max_price_cents"]
            ):
                skip_reasons["price"] += 1
                continue

            # 余额
            if min_size * token_price > balance:
                skip_reasons["balance"] += 1
                continue

            # 订单簿
            checked += 1
            print(f"  [{checked}] 检查: {question[:45]}... ({outcome})")
            ob = safe_call(lambda tid=token_id: api.get_orderbook(tid), {})
            if not ob or not ob.get("bids") or not ob.get("asks"):
                skip_reasons["no_book"] += 1
                time.sleep(0.3)
                continue

            bids = ob["bids"]
            asks = ob["asks"]
            tick_size = ob.get("tick_size", "0.01")
            best_bid = float(bids[0]["price"])
            best_ask = float(asks[0]["price"])
            spread_cents = (best_ask - best_bid) * 100

            # 价差
            if spread_cents >= settings["max_spread_cents"]:
                skip_reasons["spread"] += 1
                time.sleep(0.3)
                continue

            # 实际价格范围
            if (
                best_bid * 100 < settings["min_price_cents"]
                or best_bid * 100 > settings["max_price_cents"]
            ):
                skip_reasons["price"] += 1
                time.sleep(0.3)
                continue

            required = min_size * best_bid

            # Gamma end_date (更可靠)
            gamma_end = gamma_data.get("endDate", end_date_str)
            days_left = 0
            try:
                if gamma_end:
                    end_dt = datetime.fromisoformat(gamma_end.replace("Z", "+00:00"))
                    days_left = (end_dt.timestamp() - time.time()) / 86400
            except Exception:
                days_left = 999

            # URL
            if gamma_slug:
                url = f"https://polymarket.com/market/{gamma_slug}"
            elif market_slug:
                url = f"https://polymarket.com/market/{market_slug}"
            else:
                url = "https://polymarket.com"

            checks = [
                ("✓", f"每日奖励 ${daily_rate:.2f} ≥ ${settings['min_reward_usd']}"),
                (
                    "✓",
                    f"价差 {spread_cents:.2f}美分 < {settings['max_spread_cents']}美分",
                ),
                (
                    "✓",
                    f"价格 {best_bid * 100:.1f}美分 在 [{settings['min_price_cents']}, {settings['max_price_cents']}]",
                ),
                ("✓", f"剩余 {days_left:.0f}天 ≥ {settings['min_settlement_days']}天"),
                ("✓", f"所需资金 {required:.2f} ≤ 余额 {balance:.2f} pUSD"),
            ]

            matched.append(
                {
                    "question": question,
                    "outcome": outcome,
                    "url": url,
                    "daily_rate": daily_rate,
                    "max_spread": max_spread,
                    "min_size": min_size,
                    "days_left": days_left,
                    "end_date": gamma_end or end_date_str,
                    "tick_size": tick_size,
                    "spread_cents": spread_cents,
                    "required": required,
                    "bids": [
                        {"price": b["price"], "size": b["size"]} for b in bids[:5]
                    ],
                    "asks": [
                        {"price": a["price"], "size": a["size"]} for a in asks[:5]
                    ],
                    "checks": checks,
                }
            )

            time.sleep(0.3)

    # 结果
    print(f"\n{'─' * 70}")
    print(f"扫描完成: 检查了 {checked} 个订单簿，找到 {len(matched)} 个符合条件的市场")
    print(f"跳过原因:")
    print(f"  不在Gamma结果中(结算太近): {skip_reasons['no_gamma']}")
    print(f"  奖励不足: {skip_reasons['reward']}")
    print(f"  价格超范围: {skip_reasons['price']}")
    print(f"  余额不足: {skip_reasons['balance']}")
    print(f"  价差过大: {skip_reasons['spread']}")
    print(f"  无订单簿: {skip_reasons['no_book']}")

    for idx, m in enumerate(matched, 1):
        display_market(idx, m)

    if not matched:
        print("\n没有找到完全符合条件的市场。建议放宽条件后重试。")

    db.close()


if __name__ == "__main__":
    main()
