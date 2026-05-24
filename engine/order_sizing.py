"""下单量计算:按模式决定每笔买单挂多少份。

纯函数,不触网。place_orders 读设置后按市场调用。
"""


def compute_order_size(
    mode: str,
    order_price: float,
    balance: float,
    min_size: int,
    custom_usd: float,
) -> int | None:
    """返回应下单的份额(int),或 None 表示跳过该市场。

    - order_price <= 0 -> None(任何模式;价格非正不可能成单)。
    - "min":     返回 min_size。恒满足奖励门槛;能否买得起由 place_orders
                 里已有的 min_cost 门槛在前面拦,这里不再判余额。
    - "custom":  预算 = min(custom_usd, balance),按美元上限下单但不超过余额。
    - "balance": 预算 = balance,全额下单。
    - "custom"/"balance":size = floor(预算 / order_price);若 size < min_size
                 则返回 None(份额不够拿奖励,挂了也吃不到 -> 跳过)。
    - 未知 mode -> 按 "min" 处理(安全兜底)。
    """
    if order_price <= 0:
        return None
    if mode not in ("custom", "balance"):
        # "min" 和任何未知模式都按最小合格份额处理(安全兜底)。
        return min_size
    budget = balance if mode == "balance" else min(custom_usd, balance)
    size = int(budget / order_price)
    if size < min_size:
        return None
    return size
