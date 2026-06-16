"""test_real_order.py — 真实挂单测试：从数据库读取 eligible 列表，挂 3 笔真实买单。"""

import time
import hashlib
from models.database import Database
from utils.crypto import decrypt, derive_key
from config import DB_PATH

# determine_order_price retired in SP2; this script needs updating for laddering


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
    print("Polymarket 做市助手 — 真实挂单测试（3 笔）")
    print("=" * 80)
    print("⚠ 注意：这会用真实资金在 Polymarket 上挂买单！")
    print()

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

    api = PolymarketAPI(private_key, funder=w.get("funder") or None)

    balance = safe_call(api.get_balance, 0)
    print(f"\n钱包: {api.get_address()}")
    print(f"余额: {balance:.2f} pUSD")

    # 从数据库读取 eligible 列表
    eligible = db.get_eligible_markets()
    print(f"从数据库加载 {len(eligible)} 个 eligible 市场")

    if not eligible:
        print("数据库中没有 eligible 市场，请先在网页端点击「扫描市场」")
        db.close()
        return

    # 按 competitiveness 升序（低竞争优先）
    eligible.sort(key=lambda m: float(m.get("market_competitiveness", 0) or 0))

    # 展示候选列表
    print(f"\n{'─' * 80}")
    print(
        f"{'#':<4} {'市场名称':<35} {'方向':<8} {'奖励':<12} {'挂单价':<10} {'数量':<8} {'所需资金':<10}"
    )
    print(f"{'─' * 80}")
    for idx, m in enumerate(eligible[:10], 1):
        required = m["order_size"] * m["order_price"]
        print(
            f"{idx:<4} {m['market_name'][:33]:<35} {m['outcome']:<8} ${m['daily_reward']:.0f}/天{'':<4} {m['order_price']:.4f}{'':<4} {m['order_size']:<8} {required:.2f}"
        )
    print(f"{'─' * 80}")

    # 选择要挂单的市场
    print(f"\n将从以上列表中挂前 3 个（余额足够的）。")
    confirm = input("确认挂单？输入 yes 继续: ")
    if confirm.strip().lower() != "yes":
        print("已取消")
        db.close()
        return

    # 真实挂单
    placed = 0
    placed_orders = []

    for market in eligible:
        if placed >= 3:
            break

        # 重新读取余额
        balance = safe_call(api.get_balance, 0)
        required = market["order_size"] * market["order_price"]
        if required > balance:
            print(
                f"\n  跳过: {market['market_name'][:40]} — 余额不足 ({balance:.2f} < {required:.2f})"
            )
            continue

        # 重新拉订单簿确认价格仍然有效
        ob = safe_call(lambda tid=market["token_id"]: api.get_orderbook(tid), {})
        if not ob or not ob.get("bids") or not ob.get("asks"):
            print(f"\n  跳过: {market['market_name'][:40]} — 无订单簿")
            continue

        bids = sorted(ob["bids"], key=lambda x: float(x["price"]), reverse=True)
        asks = sorted(ob["asks"], key=lambda x: float(x["price"]))
        tick_size = float(ob.get("tick_size", "0.01"))
        tick_size_str = ob.get("tick_size", "0.01")
        best_bid = float(bids[0]["price"])
        best_ask = float(asks[0]["price"])
        midpoint = (best_bid + best_ask) / 2
        max_spread_reward = int(market.get("rewards_max_spread", 2))
        reward_range_min = midpoint - max_spread_reward * tick_size
        reward_range_max = midpoint + max_spread_reward * tick_size

        # TODO(SP2): 挂单定价改用 laddering 引擎，此脚本暂未更新
        print(
            f"\n  跳过: {market['market_name'][:40]} — test_real_order 需更新为多档引擎"
        )
        continue

        order_size = market["order_size"]
        neg_risk = bool(market.get("neg_risk", 0))

        print(f"\n{'═' * 80}")
        print(f"  挂单 #{placed + 1}")
        print(f"  市场:     {market['market_name']}")
        print(f"  方向:     {market['outcome']}")
        print(f"  token_id: {market['token_id']}")
        print(f"  买一:     {bids[0]['price']} x {bids[0]['size']}")
        print(f"  卖一:     {asks[0]['price']} x {asks[0]['size']}")
        print(f"  价差:     {(best_ask - best_bid) * 100:.2f} 美分")
        print(f"  挂单价:   {order_price:.4f}")
        print(f"  数量:     {order_size}")
        print(f"  tick_size: {tick_size_str}")
        print(f"  neg_risk: {neg_risk}")
        print(f"  所需资金: {order_size * order_price:.2f} pUSD")
        print(f"  当前余额: {balance:.2f} pUSD")

        # 下单
        print(f"  正在下单...")
        try:
            resp = api.place_limit_buy(
                market["token_id"],
                order_price,
                order_size,
                tick_size=tick_size_str,
                neg_risk=neg_risk,
            )
            order_id = resp.get("orderID", "unknown")
            status = resp.get("status", "unknown")
            print(f"  ✓ 下单成功！")
            print(f"    orderID: {order_id}")
            print(f"    status:  {status}")
            print(f"    完整响应: {resp}")

            placed_orders.append(
                {
                    "order_id": order_id,
                    "market_name": market["market_name"],
                    "outcome": market["outcome"],
                    "price": order_price,
                    "size": order_size,
                }
            )
            placed += 1
        except Exception as e:
            print(f"  ✗ 下单失败: {e}")

        time.sleep(1)

    # 汇总
    print(f"\n{'═' * 80}")
    print(f"挂单完成: 成功 {len(placed_orders)} 笔")
    print(f"{'═' * 80}")
    for idx, o in enumerate(placed_orders, 1):
        print(
            f"  #{idx} {o['market_name'][:40]} [{o['outcome']}] @ {o['price']:.4f} x {o['size']} — orderID: {o['order_id']}"
        )

    if placed_orders:
        print(f"\n等待 5 秒后检查订单状态...")
        time.sleep(5)
        for o in placed_orders:
            try:
                status = api.get_order(o["order_id"])
                print(f"\n  orderID: {o['order_id']}")
                print(f"  status:  {status.get('status', '?')}")
                print(f"  size_matched: {status.get('size_matched', '?')}")
            except Exception as e:
                print(f"\n  orderID: {o['order_id']} — 查询失败: {e}")

        # 批量撤单测试
        order_ids = [o["order_id"] for o in placed_orders if o["order_id"] != "unknown"]
        print(f"\n{'═' * 80}")
        print(f"批量撤单测试: 一次性撤销 {len(order_ids)} 笔订单")
        print(f"{'═' * 80}")
        print(f"  order_ids: {order_ids}")
        try:
            resp = api.cancel_orders(order_ids)
            print(f"  ✓ 批量撤单请求成功")
            print(f"    完整响应: {resp}")
        except Exception as e:
            print(f"  ✗ 批量撤单失败: {e}")

        print(f"\n等待 5 秒后确认订单已撤销...")
        time.sleep(5)
        for o in placed_orders:
            try:
                status = api.get_order(o["order_id"])
                print(
                    f"\n  orderID: {o['order_id']}"
                    f"\n  status:  {status.get('status', '?')}"
                    f"  (期望 CANCELED)"
                )
            except Exception as e:
                print(f"\n  orderID: {o['order_id']} — 查询失败: {e}")

    db.close()
    print("\n测试完成。")


if __name__ == "__main__":
    main()
