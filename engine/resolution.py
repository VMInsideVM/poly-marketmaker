"""engine/resolution.py — UMA 结算状态判定(纯函数,无 IO)。

Polymarket 经 UMA 乐观预言机结算。Gamma 市场对象的 ``umaResolutionStatus``
在正常交易时为 None,一旦有人在 UMA 上提交 resolution 即变为非空
(``proposed`` -> ``disputed`` -> ``resolved``)。买单不应挂在已进入结算的市场上
(随时会结算,被成交等于接盘将定价为 0/1 的份额),故据此撤买单 + 阻止重挂。
"""


def in_resolution(uma_status) -> bool:
    """umaResolutionStatus 非空(proposed/disputed/resolved)即视为已进入 UMA 结算。

    None / "" / 纯空白 -> False(正常交易,可挂买单);任何非空字符串 -> True。
    判定集中在此一处,将来若要扩成「含 closed/acceptingOrders」只改这里。
    """
    return bool(uma_status and str(uma_status).strip())
