"""engine/blacklist_ops.py — 黑名单相关纯逻辑(无 I/O,便于单测)。"""


def buy_order_ids_for_condition(orders: list, condition_id: str) -> list:
    """从一个钱包的 open orders 里挑出该 condition_id 的 BUY 单 id。

    只挑 side==BUY 且 market==condition_id 且有 id 的单;
    SELL(止盈卖单)和其它市场的单不挑。
    """
    return [
        o["id"]
        for o in orders
        if o.get("side") == "BUY" and o.get("market") == condition_id and o.get("id")
    ]
