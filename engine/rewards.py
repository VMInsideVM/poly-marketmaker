# engine/rewards.py
"""Pure parsing of /rewards/markets/{condition_id} response (no IO)."""

from typing import Optional


def extract_max_spread(rewards_items: list) -> Optional[int]:
    """Parse rewards_max_spread from get_rewards_for_market()'s return.

    The wrapper returns the response's ``data`` list; ``rewards_max_spread``
    sits at each item's top level (verified against a live response).

    Returns the first item's valid int rewards_max_spread, or None when the
    list is empty / no item carries the field / the value can't be int()'d.
    Callers treat None as "couldn't determine — skip safely".
    """
    for it in rewards_items or []:
        if not isinstance(it, dict):
            continue
        v = it.get("rewards_max_spread")
        if v is None:
            continue
        try:
            return int(float(v))
        except (TypeError, ValueError):
            continue
    return None
