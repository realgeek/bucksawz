"""
Estimate monthly costs for usage-based cost components using CloudWatch actuals.

Strategy
--------
- For each usage-based CostComponent (monthlyCost is None, price is not None),
  match the component to a CloudWatch metric from `cw_actuals`.
- Compute estimated_cost = normalized_monthly_quantity × price.

CloudWatch quantity semantics (from cloudwatch.py)
---------------------------------------------------
ConsumedLCUs  — Average LCUs/hour over the lookback period.
               Monthly estimate = avg_lcu × 730 hrs/month × price_per_lcu.
Requests      — Total millions of requests over the lookback period (SQS, Lambda, APIGW).
Invocations   — Same as Requests; normalised to millions before storage.
               Monthly estimate = (total_millions / months_elapsed) × price_per_million.
"""
from __future__ import annotations
from typing import Optional
from ..schema.infracost import Resource


def estimate_resource_cost(
    resource: Resource,
    cw_actuals: dict,
    lookback_days: int = 90,
) -> Optional[float]:
    """
    Returns estimated monthly cost (USD) for usage-based components, or None
    if estimation is not possible (no actuals or no priceable usage-based components).
    """
    if not cw_actuals:
        return None

    months = max(lookback_days / 30, 1.0)
    total = 0.0
    estimated_any = False

    for comp in resource.cost_components:
        if not comp.usage_based or comp.price is None:
            continue
        est = _estimate_component(
            price=comp.price,
            unit=comp.unit.lower(),
            name=comp.name.lower(),
            actuals=cw_actuals,
            months=months,
        )
        if est is not None:
            total += est
            estimated_any = True

    return round(total, 6) if estimated_any else None


def _estimate_component(
    price: float,
    unit: str,
    name: str,
    actuals: dict,
    months: float,
) -> Optional[float]:
    # ── ALB / NLB LCU ────────────────────────────────────────────────────────
    if "lcu" in unit or "capacity unit" in name:
        lcu_avg = actuals.get("ConsumedLCUs")
        if lcu_avg is not None:
            return lcu_avg * 730.0 * price

    # ── Request-based: SQS / Lambda / API Gateway ─────────────────────────────
    # unit is "1M requests", "1M queries", etc.
    if "request" in unit or ("request" in name and "gb" not in unit):
        req_millions = actuals.get("Requests") or actuals.get("Invocations")
        if req_millions is not None:
            monthly_millions = req_millions / months
            return monthly_millions * price

    return None


def estimate_all(
    resources: list[Resource],
    cw_actuals: dict[str, dict],
    lookback_days: int = 90,
) -> dict[str, float]:
    """
    Estimate costs for every resource in `resources` where CW actuals exist.
    Returns {resource_name: estimated_monthly_cost_usd}.
    Resources with no estimable components or no actuals are omitted.
    """
    results: dict[str, float] = {}
    for r in resources:
        actuals = cw_actuals.get(r.name, {})
        est = estimate_resource_cost(r, actuals, lookback_days)
        if est is not None:
            results[r.name] = est
    return results
