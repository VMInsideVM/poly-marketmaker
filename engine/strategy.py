"""engine/strategy.py — Order placement strategy based on orderbook depth."""


def determine_order_price(
    bids: list[dict],
    max_spread: int,
    tick_size: float,
    reward_range_min: float,
    reward_range_max: float,
) -> float | None:
    """Determine the price at which to place a buy order.

    Args:
        bids: Sorted list of bid levels [{price, size}, ...], highest price first.
        max_spread: Reward max spread parameter (number of ticks).
        tick_size: Price tick size (0.01 for 1-cent, 0.001 for 0.1-cent).
        reward_range_min: Minimum price in reward range.
        reward_range_max: Maximum price in reward range.

    Returns:
        Price to place order at, or None if no valid position found.
    """
    if not bids:
        return None

    is_fine_tick = tick_size < 0.01  # 0.1-cent increments

    if is_fine_tick:
        price = _strategy_cumulative(bids, reward_range_min, reward_range_max)
    elif max_spread == 2:
        price = _strategy_spread2_coarse(bids, reward_range_min, reward_range_max)
    else:
        price = _strategy_spread_ge3_coarse(
            bids, max_spread, tick_size, reward_range_min, reward_range_max
        )

    return price


def _strategy_cumulative(
    bids: list[dict],
    reward_range_min: float,
    reward_range_max: float,
    threshold: int = 6000,
) -> float | None:
    """0.1-cent tick strategy: find cumulative > threshold, return next position."""
    cumulative = 0
    for i, bid in enumerate(bids):
        cumulative += int(bid["size"])
        if cumulative > threshold:
            # Place at the next position (one level deeper)
            if i + 1 < len(bids):
                target = bids[i + 1]["price"]
            else:
                # No next level exists
                return None
            target = float(target)
            if reward_range_min <= target <= reward_range_max:
                return target
            return None
    return None


def _strategy_spread2_coarse(
    bids: list[dict],
    reward_range_min: float,
    reward_range_max: float,
) -> float | None:
    """max_spread=2, 1-cent tick: bid1 > 2000 -> bid2, else skip."""
    if not bids:
        return None

    bid1_size = int(bids[0]["size"])
    if bid1_size > 2000:
        if len(bids) < 2:
            return None
        target = float(bids[1]["price"])
        if reward_range_min <= target <= reward_range_max:
            return target
    # bid1 ≤ 2000: don't place order
    return None


def _strategy_spread_ge3_coarse(
    bids: list[dict],
    max_spread: int,
    tick_size: float,
    reward_range_min: float,
    reward_range_max: float,
) -> float | None:
    """max_spread>=3, 1-cent tick: find level with size > 2000, place one below."""
    best_bid_price = float(bids[0]["price"])
    min_price = best_bid_price - max_spread * tick_size

    for i, bid in enumerate(bids):
        bid_price = float(bid["price"])
        bid_size = int(bid["size"])

        if bid_price < min_price:
            break  # Exceeded max_spread, stop

        if bid_size > 2000:
            # Place at the next level
            if i + 1 < len(bids):
                target = float(bids[i + 1]["price"])
                if (
                    target >= min_price
                    and reward_range_min <= target <= reward_range_max
                ):
                    return target
            return None

    return None
