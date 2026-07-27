"""Tests for AWS Support plan pricing (tier ladder, floors, and deltas)."""
import pytest
from bucksawz.pricing.support import (
    PLANS, effective_rate, is_floor_bound, plan_minimum, plan_name,
    support_cost, support_delta,
)


# ── Business tiers ───────────────────────────────────────────────────────────


def test_business_first_tier_is_flat_ten_percent():
    assert support_cost(6_000.0, "business") == pytest.approx(600.0)


def test_business_floor_applies_below_one_thousand():
    """10% of $500 is $50, under the $100 minimum."""
    assert support_cost(500.0, "business") == pytest.approx(100.0)


def test_business_floor_stops_binding_at_one_thousand():
    assert support_cost(1_000.0, "business") == pytest.approx(100.0)
    assert support_cost(1_500.0, "business") == pytest.approx(150.0)


def test_business_blends_across_first_boundary():
    """$20k = 10% of the first $10k + 7% of the next $10k."""
    assert support_cost(20_000.0, "business") == pytest.approx(1_000.0 + 700.0)


def test_business_blends_across_all_tiers():
    # 10%×10k + 7%×70k + 5%×170k + 3%×50k
    expected = 1_000.0 + 4_900.0 + 8_500.0 + 1_500.0
    assert support_cost(300_000.0, "business") == pytest.approx(expected)


def test_business_zero_usage_still_costs_the_minimum():
    assert support_cost(0.0, "business") == pytest.approx(100.0)


# ── Other plans ──────────────────────────────────────────────────────────────


def test_developer_is_flat_three_percent_over_its_floor():
    assert support_cost(10_000.0, "developer") == pytest.approx(300.0)
    assert support_cost(100.0, "developer") == pytest.approx(29.0)


def test_enterprise_onramp_floor_dominates_small_usage():
    assert support_cost(6_000.0, "enterprise-onramp") == pytest.approx(5_500.0)


def test_enterprise_uses_wider_first_tier():
    """Enterprise charges 10% up to $150k, where Business has already stepped down."""
    assert support_cost(100_000.0, "enterprise") == pytest.approx(15_000.0)
    assert support_cost(100_000.0, "business") < support_cost(100_000.0, "enterprise")


def test_plan_lookup_is_case_insensitive():
    assert support_cost(6_000.0, "Business") == pytest.approx(600.0)


def test_unknown_plan_raises():
    with pytest.raises(ValueError, match="unknown support plan"):
        support_cost(6_000.0, "platinum")


def test_plan_name_and_registry():
    assert plan_name("business") == "Business"
    assert set(PLANS) == {"developer", "business", "enterprise-onramp", "enterprise"}


# ── Deltas ───────────────────────────────────────────────────────────────────


def test_delta_within_a_tier_is_the_flat_rate():
    """$6k + $42.05/mo of new infrastructure costs 10% more in support."""
    assert support_delta(6_000.0, 6_042.05, "business") == pytest.approx(4.205)


def test_delta_across_a_boundary_blends_rates():
    """$9.5k → $10.5k: $500 at 10%, $500 at 7% — not $1000 at either rate."""
    delta = support_delta(9_500.0, 10_500.0, "business")
    assert delta == pytest.approx(50.0 + 35.0)
    assert delta < 1_000.0 * 0.10


def test_delta_below_the_floor_is_zero():
    """Under the minimum, extra usage adds nothing until the percentage overtakes it."""
    assert support_delta(200.0, 400.0, "business") == pytest.approx(0.0)


def test_delta_is_negative_when_tearing_down():
    assert support_delta(6_000.0, 5_000.0, "business") == pytest.approx(-100.0)


def test_delta_is_zero_for_no_change():
    assert support_delta(6_000.0, 6_000.0, "business") == pytest.approx(0.0)


# ── Effective rate ───────────────────────────────────────────────────────────


def test_effective_rate_matches_headline_in_first_tier():
    assert effective_rate(6_000.0, "business") == pytest.approx(0.10)


def test_effective_rate_declines_past_the_first_tier():
    assert effective_rate(80_000.0, "business") < 0.10


def test_effective_rate_none_at_zero_usage():
    assert effective_rate(0.0, "business") is None


def test_effective_rate_is_misleading_below_the_floor():
    """Arithmetically correct, which is why is_floor_bound exists to suppress it."""
    assert effective_rate(137.46, "business") > 0.7
    assert is_floor_bound(137.46, "business")


# ── Floor detection ──────────────────────────────────────────────────────────


def test_floor_bound_below_the_crossover():
    assert is_floor_bound(500.0, "business")


def test_not_floor_bound_above_the_crossover():
    assert not is_floor_bound(6_000.0, "business")


def test_floor_bound_at_the_exact_crossover():
    """10% of $1,000 is exactly the $100 minimum."""
    assert is_floor_bound(1_000.0, "business")


def test_plan_minimum():
    assert plan_minimum("business") == pytest.approx(100.0)
    assert plan_minimum("enterprise") == pytest.approx(15_000.0)
