"""test_simulate.py — 模拟挂单：遍历 eligible 列表，展示订单簿和模拟订单详情，最后汇总报告。"""

import time
import hashlib
from models.database import Database
from utils.crypto import decrypt, derive_key
from config import DB_PATH


def safe_call(fn, default=None, retries=3):
    for i in range(retries):
        try:
            return fn()
        except Exception as e:
            if i < retries - 1:
                time.sleep(1)
            else:
                print(f"  API 失败: {e}")
                return default


def main():
    print("=" * 80)
    print("Polymarket 做市助手 — 模拟挂单测试")
    print("=" * 80)

    db = Database(DB_PATH)
    db.init()
    pw_hash, salt = db.get_password()
    if pw_hash is None:
        print("未设置密码")
        return
    password = input("请输入访问密码: ")
    key = derive_key(password, salt)
    if hashlib.sha256(key).hexdigest() != pw_hash:
        print("密码错误")
        return

    wallets = db.list_wallets()
    if not wallets:
        print("没有钱包")
        return

    w = wallets[0]
    private_key = decrypt(w["encrypted_key"], key)

    from api.polymarket_api import PolymarketAPI

    # determine_order_price retired in SP2; this script needs updating for laddering

    api = PolymarketAPI(private_key, funder=w.get("funder") or None)
    balance = safe_call(api.get_balance, 0)
    print(f"\n钱包: {api.get_address()}")
    print(f"余额: {balance:.2f} pUSD")

    eligible = db.get_eligible_markets()
    print(f"从数据库加载 {len(eligible)} 个 eligible 市场")

    if not eligible:
        print("数据库中没有 eligible 市场，请先在网页端点击「扫描市场」")
        db.close()
        return

    # 收集结果
    success_list = []  # 模拟挂单成功
    skipped_list = []  # 被跳过的

    for idx, market in enumerate(eligible, 1):
        print(f"\n{'═' * 80}")
        print(f"#{idx}/{len(eligible)} 模拟挂单")
        print(f"{'═' * 80}")
        print(f"  市场:       {market['market_name']}")
        print(f"  方向:       {market['outcome']}")
        print(f"  market_id:  {market['market_id']}")
        print(f"  token_id:   {market['token_id']}")
        print(f"  每日奖励:   ${market['daily_reward']:.2f}/天")
        print(f"  max_spread: {market['rewards_max_spread']}")
        print(f"  min_size:   {market['rewards_min_size']}")
        print(f"  tick_size:  {market['tick_size']}")

        # 拉订单簿
        ob = safe_call(lambda tid=market["token_id"]: api.get_orderbook(tid), {})
        time.sleep(0.3)

        if not ob or not ob.get("bids") or not ob.get("asks"):
            reason = "无法获取订单簿"
            print(f"  ✗ {reason}")
            skipped_list.append({"market": market, "reason": reason})
            continue

        bids = sorted(ob["bids"], key=lambda x: float(x["price"]), reverse=True)
        asks = sorted(ob["asks"], key=lambda x: float(x["price"]))
        best_bid = float(bids[0]["price"])
        best_ask = float(asks[0]["price"])
        spread = (best_ask - best_bid) * 100

        # 订单簿
        print(f"\n  【当前订单簿】 (价差: {spread:.2f}美分)")
        print(f"  ┌{'─' * 32}┬{'─' * 32}┐")
        print(f"  │ {'买盘 (Bids)':^30} │ {'卖盘 (Asks)':^30} │")
        print(f"  │ {'价格':>12}  {'数量':<14} │ {'价格':>12}  {'数量':<14} │")
        print(f"  ├{'─' * 32}┼{'─' * 32}┤")
        max_rows = max(len(bids[:10]), len(asks[:10]))
        for r in range(max_rows):
            bid_str = (
                f" {bids[r]['price']:>12}  {bids[r]['size']:<14}"
                if r < len(bids)
                else " " * 30
            )
            ask_str = (
                f" {asks[r]['price']:>12}  {asks[r]['size']:<14}"
                if r < len(asks)
                else " " * 30
            )
            print(f"  │ {bid_str:30} │ {ask_str:30} │")
        print(f"  └{'─' * 32}┴{'─' * 32}┘")

        # 策略计算
        tick_size = float(ob.get("tick_size", "0.01"))
        midpoint = (best_bid + best_ask) / 2
        max_spread_reward = int(market["rewards_max_spread"])
        reward_range_min = midpoint - max_spread_reward * tick_size
        reward_range_max = midpoint + max_spread_reward * tick_size

        print(f"\n  【策略分析】")
        print(f"    midpoint:          {midpoint:.4f}")
        print(f"    tick_size:         {tick_size}")
        print(f"    reward_max_spread: {max_spread_reward} ticks")
        print(
            f"    reward_range:      [{reward_range_min:.4f}, {reward_range_max:.4f}]"
        )

        # 推导过程
        print(f"\n  【挂单位置推导】")
        is_fine_tick = tick_size < 0.01
        if is_fine_tick:
            print(f"    策略: 0.1美分精度 → 累计份额 > 6000 的下一个位置")
            cumulative = 0
            for i, bid in enumerate(bids[:10]):
                cumulative += int(float(bid["size"]))
                marker = " ← 累计超过6000" if cumulative > 6000 else ""
                print(
                    f"    #{i+1} price={bid['price']} size={bid['size']} 累计={cumulative}{marker}"
                )
        else:
            if max_spread_reward == 2:
                print(f"    策略: max_spread=2, 1美分精度")
                bid1_size = int(float(bids[0]["size"]))
                print(f"    买一: price={bids[0]['price']}, size={bid1_size}")
                if bid1_size > 2000:
                    print(f"    买一份额 {bid1_size} > 2000 → 挂买二位置")
                else:
                    print(f"    买一份额 {bid1_size} ≤ 2000 → 不挂单")
            else:
                print(
                    f"    策略: max_spread≥3, 1美分精度 → 找到 > 2000 的档位，挂下一档"
                )
                for i, bid in enumerate(bids[:10]):
                    bid_size = int(float(bid["size"]))
                    marker = " ← > 2000, 挂下一档" if bid_size > 2000 else ""
                    print(f"    #{i+1} price={bid['price']} size={bid_size}{marker}")

        # TODO(SP2): 定价改用 laddering 引擎，此脚本暂未更新
        reason = "test_simulate 需更新为多档引擎（determine_order_price 已退役）"
        print(f"\n  ✗ {reason}")
        skipped_list.append({"market": market, "reason": reason})
        continue

        order_size = market["order_size"]

        # 判定结果
        print(f"\n  【模拟订单】")
        if order_price is None:
            reason = "策略未找到合适挂单价格"
            # 具体原因分析
            if is_fine_tick:
                reason += " (累计份额未超过6000或下一个位置超出reward range)"
            elif max_spread_reward == 2:
                bid1_size = int(float(bids[0]["size"]))
                if bid1_size <= 2000:
                    reason += f" (买一份额={bid1_size} ≤ 2000, max_spread=2时不挂)"
                else:
                    reason += f" (买二价格超出reward range)"
            else:
                reason += " (所有档位均 ≤ 2000 或目标价超出reward range/max_spread)"
            print(f"    ✗ {reason}")
            skipped_list.append({"market": market, "reason": reason})
            continue

        required = order_size * order_price
        in_range = reward_range_min <= order_price <= reward_range_max
        can_afford = required <= balance

        print(f"    挂单价格:     {order_price:.4f}")
        print(f"    挂单数量:     {order_size}")
        print(f"    所需资金:     {required:.2f} pUSD")
        print(
            f"    在奖励范围内: {'是' if in_range else '否'} [{reward_range_min:.4f}, {reward_range_max:.4f}]"
        )
        print(f"    余额足够:     {'是' if can_afford else '否'} (余额: {balance:.2f})")

        if not in_range:
            reason = f"挂单价格 {order_price:.4f} 不在奖励范围 [{reward_range_min:.4f}, {reward_range_max:.4f}]"
            print(f"\n    >>> 模拟挂单失败: {reason} <<<")
            skipped_list.append({"market": market, "reason": reason})
        elif not can_afford:
            reason = f"余额不足 ({balance:.2f} < {required:.2f})"
            print(f"\n    >>> 模拟挂单失败: {reason} <<<")
            skipped_list.append({"market": market, "reason": reason})
        else:
            print(f"\n    >>> 模拟挂单成功！BUY {order_size} @ {order_price:.4f} <<<")
            success_list.append(
                {
                    "market_name": market["market_name"],
                    "outcome": market["outcome"],
                    "order_price": order_price,
                    "order_size": order_size,
                    "required": required,
                    "daily_reward": market["daily_reward"],
                    "competitiveness": market.get("market_competitiveness", 0),
                }
            )

    # ================================================================
    # 汇总报告
    # ================================================================
    print(f"\n\n{'█' * 80}")
    print(f"  汇总报告")
    print(f"{'█' * 80}")
    print(f"  eligible 总数:    {len(eligible)}")
    print(f"  模拟挂单成功:     {len(success_list)}")
    print(f"  被跳过:           {len(skipped_list)}")

    # 成功的
    if success_list:
        print(f"\n{'─' * 80}")
        print(f"  ✓ 模拟挂单成功 ({len(success_list)} 笔)")
        print(f"{'─' * 80}")
        print(
            f"  {'#':<4} {'市场名称':<30} {'方向':<8} {'挂单价':<10} {'数量':<8} {'资金':<10} {'奖励':<12} {'竞争度'}"
        )
        print(f"  {'─' * 78}")
        for i, s in enumerate(success_list, 1):
            print(
                f"  {i:<4} {s['market_name'][:28]:<30} {s['outcome']:<8} {s['order_price']:.4f}{'':<4} {s['order_size']:<8} {s['required']:.2f}{'':<4} ${s['daily_reward']:.0f}/天{'':<4} {s['competitiveness']}"
            )

    # 跳过的
    if skipped_list:
        print(f"\n{'─' * 80}")
        print(f"  ✗ 被跳过的市场 ({len(skipped_list)} 个)")
        print(f"{'─' * 80}")
        print(f"  {'#':<4} {'市场名称':<30} {'方向':<8} {'跳过原因'}")
        print(f"  {'─' * 78}")
        for i, s in enumerate(skipped_list, 1):
            m = s["market"]
            print(
                f"  {i:<4} {m['market_name'][:28]:<30} {m['outcome']:<8} {s['reason']}"
            )

        # 按跳过原因分类统计
        reason_counts = {}
        for s in skipped_list:
            # 提取原因关键词
            r = s["reason"]
            if "订单簿" in r:
                cat = "无法获取订单簿"
            elif "未找到合适挂单价格" in r:
                cat = "策略未找到挂单价格"
            elif "不在奖励范围" in r:
                cat = "挂单价格超出奖励范围"
            elif "余额不足" in r:
                cat = "余额不足"
            elif "策略计算异常" in r:
                cat = "策略计算异常"
            else:
                cat = "其他"
            reason_counts[cat] = reason_counts.get(cat, 0) + 1

        print(f"\n  跳过原因统计:")
        for cat, count in sorted(reason_counts.items(), key=lambda x: -x[1]):
            print(f"    {cat}: {count} 个")

    print(f"\n{'█' * 80}")
    print(f"  模拟完成")
    print(f"{'█' * 80}")
    db.close()


if __name__ == "__main__":
    main()
