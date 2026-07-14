"""engine/tiers.py — 档位模块(size_tiers)解析与校验纯函数(不触网)。

每个模块对「最低奖励份额 = size」的市场精确生效,携带该档的挂单份数与
选档参数(规则1/2/3门槛、高位系数和门槛、金额数值表)。无匹配 -> 不挂。
"""

_TIER_COEFF_KEYS = (
    "rule1_min_coeff",
    "rule2_min_coeff",
    "rule3_min_coeff",
    "gap_high_coeff_sum_min",
)


def enabled_sizes(size_tiers) -> set:
    """已启用模块的档位值集合(int)。非法条目跳过。"""
    out = set()
    for t in size_tiers or []:
        try:
            if t.get("enabled", False):
                out.add(int(t.get("size")))
        except (AttributeError, TypeError, ValueError):
            continue
    return out


def tier_for(size_tiers, min_size):
    """精确匹配:返回 enabled 且 size == min_size 的模块;无 -> None。"""
    try:
        ms = int(min_size)
    except (TypeError, ValueError):
        return None
    for t in size_tiers or []:
        try:
            if t.get("enabled", False) and int(t.get("size", -1)) == ms:
                return t
        except (AttributeError, TypeError, ValueError):
            continue
    return None


def validate_size_tiers(raw):
    """校验并归一化 size_tiers。返回 (归一化列表, None) 或 (None, 中文错误)。"""
    if not isinstance(raw, list):
        return None, "size_tiers 必须是数组"
    out, seen = [], set()
    for i, t in enumerate(raw):
        if not isinstance(t, dict):
            return None, f"第 {i + 1} 个档位不是对象"
        try:
            size_raw = t.get("size")
            shares_raw = t.get("shares")
            size = int(size_raw)
            shares = int(shares_raw)
            if float(size_raw) != size or float(shares_raw) != shares:
                return None, f"第 {i + 1} 个档位的档位值/挂单份数必须是整数"
        except (TypeError, ValueError, OverflowError):
            return None, f"第 {i + 1} 个档位的档位值/挂单份数必须是整数"
        if size <= 0:
            return None, f"第 {i + 1} 个档位的档位值必须为正整数"
        if size in seen:
            return None, f"档位值 {size} 重复"
        seen.add(size)
        if shares < size:
            return None, f"档位 {size} 的挂单份数({shares})不能小于档位值"
        norm = {"size": size, "enabled": bool(t.get("enabled", True)), "shares": shares}
        for k in _TIER_COEFF_KEYS:
            try:
                v = float(t.get(k, 0) or 0)
            except (TypeError, ValueError):
                return None, f"档位 {size} 的 {k} 必须是数字"
            if v < 0:
                return None, f"档位 {size} 的 {k} 不能为负"
            norm[k] = v
        table = t.get("amount_value_table") or []
        if not isinstance(table, list):
            return None, f"档位 {size} 的金额数值表格式错误"
        norm["amount_value_table"] = table
        out.append(norm)
    return out, None
