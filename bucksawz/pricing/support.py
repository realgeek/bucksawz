"""
AWS Support plan pricing.

Support isn't a resource — it's a percentage of monthly usage charges, tiered,
with a per-plan floor. So it's applied to a total rather than mapped from a
Terraform config, which is why it lives outside `_PRICERS`.

The delta a change causes is `support_cost(after) - support_cost(before)`, not
`delta × rate`: that falls out of the tier ladder correctly when a change
pushes usage across a tier boundary, and it respects the floor (below it, extra
usage costs nothing in support until the percentage overtakes the minimum).

Rates are the published list prices. The base is *usage* charges — support
fees, taxes and most Marketplace charges are excluded from the calculation.
"""
from __future__ import annotations
from typing import Optional

# plan -> (display name, monthly minimum USD, [(tier upper bound or None, rate)])
_PLANS: dict[str, tuple[str, float, list[tuple[Optional[float], float]]]] = {
    "developer": ("Developer", 29.0, [(None, 0.03)]),
    "business": ("Business", 100.0, [
        (10_000.0, 0.10), (80_000.0, 0.07), (250_000.0, 0.05), (None, 0.03),
    ]),
    "enterprise-onramp": ("Enterprise On-Ramp", 5_500.0, [
        (10_000.0, 0.10), (80_000.0, 0.07), (250_000.0, 0.05), (None, 0.03),
    ]),
    "enterprise": ("Enterprise", 15_000.0, [
        (150_000.0, 0.10), (500_000.0, 0.07), (1_000_000.0, 0.05), (None, 0.03),
    ]),
}

PLANS: list[str] = list(_PLANS)


def plan_name(plan: str) -> str:
    return _PLANS[plan.lower()][0]


def plan_minimum(plan: str) -> float:
    return _PLANS[plan.lower()][1]


def is_floor_bound(monthly_usage: float, plan: str = "business") -> bool:
    """
    True when the plan's minimum exceeds the percentage, so usage isn't what's
    setting the price. Below this point an effective rate is arithmetically
    correct but misleading — the charge is flat.
    """
    return support_cost(monthly_usage, plan) <= plan_minimum(plan) + 1e-9


def support_cost(monthly_usage: float, plan: str = "business") -> float:
    """Monthly support charge on `monthly_usage` of AWS usage charges."""
    try:
        _, minimum, tiers = _PLANS[plan.lower()]
    except KeyError:
        raise ValueError(f"unknown support plan '{plan}' — one of: {', '.join(PLANS)}")

    remaining = max(monthly_usage, 0.0)
    lower = 0.0
    total = 0.0
    for upper, rate in tiers:
        if remaining <= 0:
            break
        band = remaining if upper is None else min(remaining, upper - lower)
        total += band * rate
        remaining -= band
        lower = upper if upper is not None else lower
    return max(minimum, total)


def support_delta(before_usage: float, after_usage: float, plan: str = "business") -> float:
    """
    Change in support charge caused by a change in usage.

    Not `delta × rate`: crossing a tier boundary blends two rates, and below the
    plan's floor a usage increase costs nothing in support at all.
    """
    return support_cost(after_usage, plan) - support_cost(before_usage, plan)


def effective_rate(monthly_usage: float, plan: str = "business") -> Optional[float]:
    """Support as a fraction of usage, for display. None when usage is zero."""
    if monthly_usage <= 0:
        return None
    return support_cost(monthly_usage, plan) / monthly_usage
