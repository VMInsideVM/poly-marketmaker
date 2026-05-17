"""test_live.py — 展示5个奖励市场的完整参数，验证筛选逻辑。"""

import time
import traceback
from datetime import datetime
from models.database import Database
from utils.crypto import decrypt, derive_key
from config import DB_PATH
import hashlib


def parse_end_date(s):
    if not s:
        return 0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    return 0


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


def main():
    print("=" * 70)
    print("Polymarket 做市助手 — 市场参数展示（5个市场）")
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

    # 筛选条件
    settings = db.get_settings()
    print(f"\n{'─' * 70}")
    print("当前筛选条件:")
    print(f"  最低奖励 (rate_per_day)  ≥ {settings['min_reward_usd']} USD/天")
    print(f"  买卖价差上限            < {settings['max_spread_cents']} 美分")
    print(
        f"  单价范围                {settings['min_price_cents']}~{settings['max_price_cents']} 美分"
    )
    print(f"  结算日期                > {settings['min_settlement_days']} 天")

    # 获取市场（服务端预过滤价格和价差）
    print(f"\n正在获取奖励市场（服务端预过滤）...")
    markets = safe_call(
        lambda: api.get_rewards_markets(
            min_price=settings["min_price_cents"] / 100,
            max_price=settings["max_price_cents"] / 100,
            max_spread=settings["max_spread_cents"] / 100,
        ),
        [],
    )
    print(f"共获取 {len(markets)} 个奖励市场\n")

    if not markets:
        print("✗ 未获取到市场数据")
        return

    # 取前5个市场展示
    count = 0
    for market in markets:
        if count >= 5:
            break

        question = market.get("question", "N/A")
        condition_id = market.get("condition_id", "N/A")
        market_slug = market.get("market_slug", "")
        end_date_str = market.get("end_date", "")
        end_ts = parse_end_date(end_date_str)
        days_left = (end_ts - time.time()) / 86400 if end_ts else 0
        rewards_config = market.get("rewards_config", [])
        daily_rate = sum(rc.get("rate_per_day", 0) for rc in rewards_config)
        total_rewards = sum(rc.get("total_rewards", 0) for rc in rewards_config)
        max_spread = market.get("rewards_max_spread", "N/A")
        min_size = market.get("rewards_min_size", "N/A")
        tokens = market.get("tokens", [])

        if not tokens:
            continue

        token = tokens[0]  # 只看第一个token
        token_id = token.get("token_id", "")
        token_price = float(token.get("price", 0))
        outcome = token.get("outcome", "?")

        # 获取订单簿
        ob = safe_call(lambda tid=token_id: api.get_orderbook(tid), {})
        if not ob:
            continue

        bids = ob.get("bids", [])
        asks = ob.get("asks", [])
        tick_size = ob.get("tick_size", "N/A")

        best_bid = float(bids[0]["price"]) if bids else 0
        best_ask = float(asks[0]["price"]) if asks else 0
        spread_cents = (best_ask - best_bid) * 100 if best_bid and best_ask else 0

        # URL
        url = (
            f"https://polymarket.com/market/{market_slug}"
            if market_slug
            else f"https://polymarket.com"
        )

        count += 1
        print(f"{'=' * 70}")
        print(f"市场 #{count}: {question}")
        print(f"{'=' * 70}")
        print(f"  网址:        {url}")
        print(f"  condition_id: {condition_id[:30]}...")
        print(f"  方向:        {outcome}")
        print(f"  token价格:   {token_price}")
        print(f"")

        # 奖励参数
        print(f"  【奖励参数】")
        print(f"    每日奖励:     ${daily_rate:.2f}/天")
        print(f"    总奖励:       ${total_rewards:.2f}")
        print(f"    max_spread:   {max_spread}")
        print(f"    min_size:     {min_size}")
        print(f"    rewards_config 原始数据:")
        for rc in rewards_config:
            print(f"      {rc}")
        print(f"")

        # 市场参数
        print(f"  【市场参数】")
        print(f"    结算日期:     {end_date_str} (剩余 {days_left:.0f} 天)")
        print(f"    tick_size:    {tick_size}")
        print(f"    当前价差:     {spread_cents:.2f} 美分")
        print(f"")

        # 订单簿
        print(f"  【订单簿】")
        print(f"    ┌{'─' * 28}┬{'─' * 28}┐")
        print(f"    │ {'买盘 (Bids)':^26} │ {'卖盘 (Asks)':^26} │")
        print(f"    │ {'价格':>10}  {'数量':<12} │ {'价格':>10}  {'数量':<12} │")
        print(f"    ├{'─' * 28}┼{'─' * 28}┤")
        max_rows = max(len(bids[:5]), len(asks[:5]))
        for r in range(max_rows):
            if r < len(bids):
                bid_str = f" {bids[r]['price']:>10}  {bids[r]['size']:<12}"
            else:
                bid_str = " " * 26
            if r < len(asks):
                ask_str = f" {asks[r]['price']:>10}  {asks[r]['size']:<12}"
            else:
                ask_str = " " * 26
            print(f"    │ {bid_str:26} │ {ask_str:26} │")
        print(f"    └{'─' * 28}┴{'─' * 28}┘")

        # 筛选判断
        print(f"")
        print(f"  【筛选结果】")
        checks = []

        # 1. 奖励金额
        ok = daily_rate >= settings["min_reward_usd"]
        checks.append(
            (
                "✓" if ok else "✗",
                f"每日奖励 ${daily_rate:.2f} {'≥' if ok else '<'} ${settings['min_reward_usd']}",
            )
        )

        # 2. 价差
        ok = spread_cents < settings["max_spread_cents"]
        checks.append(
            (
                "✓" if ok else "✗",
                f"价差 {spread_cents:.2f}美分 {'<' if ok else '≥'} {settings['max_spread_cents']}美分",
            )
        )

        # 3. 价格范围
        price_cents = best_bid * 100
        ok = settings["min_price_cents"] <= price_cents <= settings["max_price_cents"]
        checks.append(
            (
                "✓" if ok else "✗",
                f"价格 {price_cents:.1f}美分 {'在' if ok else '不在'} [{settings['min_price_cents']}, {settings['max_price_cents']}]",
            )
        )

        # 4. 结算日期
        ok = days_left >= settings["min_settlement_days"]
        checks.append(
            (
                "✓" if ok else "✗",
                f"剩余 {days_left:.0f}天 {'≥' if ok else '<'} {settings['min_settlement_days']}天",
            )
        )

        # 5. 余额
        required = float(min_size) * best_bid if min_size != "N/A" and best_bid else 0
        ok = required <= balance
        checks.append(
            (
                "✓" if ok else "✗",
                f"所需资金 {required:.2f} {'≤' if ok else '>'} 余额 {balance:.2f} pUSD",
            )
        )

        all_pass = all(c[0] == "✓" for c in checks)
        for mark, desc in checks:
            print(f"    {mark} {desc}")
        print(f"    → {'全部通过 ✓' if all_pass else '未通过 ✗'}")
        print(f"")

        time.sleep(0.5)  # 避免请求太快

    db.close()
    print("诊断完成。")


if __name__ == "__main__":
    main()
