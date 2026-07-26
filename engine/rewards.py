# engine/rewards.py
"""Pure parsing of /rewards/markets/{condition_id} response (no IO)."""

from typing import Optional


def extract_max_spread(rewards_items: list) -> Optional[float]:
    """Parse rewards_max_spread from get_rewards_for_market()'s return.

    The wrapper returns the response's ``data`` list; ``rewards_max_spread``
    sits at each item's top level (verified against a live response).

    Returns the first item's valid rewards_max_spread **in cents** as a float,
    or None when the list is empty / no item carries the field / the value
    can't be parsed. The value is kept as a float (NOT int()'d): live values
    like 3.5 or 4.5 cents must survive, since truncating to 3/4 narrows the
    reward band. Callers treat None as "couldn't determine — skip safely".
    """
    for it in rewards_items or []:
        if not isinstance(it, dict):
            continue
        v = it.get("rewards_max_spread")
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def extract_daily_rate(rewards_items: list) -> Optional[float]:
    """Sum rate_per_day across all reward configs of /rewards/markets/{cid}.

    Returns the market's total daily reward in USD, or None when the payload
    carries no parsable rewards_config at all. 0.0 and None are different:
    0.0 means the reward really is zero (cancel the resting buy), None means
    we could not tell (skip safely). Callers must not collapse them into one
    falsy check.

    Same formula the discovery scan uses for market_reward
    (engine/scanner.py _precise_reward), so the two stay on one yardstick.
    """
    total = 0.0
    found = False
    for it in rewards_items or []:
        if not isinstance(it, dict):
            continue
        for rc in it.get("rewards_config") or []:
            if not isinstance(rc, dict):
                continue
            v = rc.get("rate_per_day")
            if v is None:
                continue
            try:
                total += float(v)
            except (TypeError, ValueError):
                continue
            found = True
    return total if found else None
