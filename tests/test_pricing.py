"""Tests for the pricing engine: SQLite DB and usage-based estimator."""
import pytest
from pathlib import Path
from bucksawz.pricing import db as price_db
from bucksawz.pricing.estimator import estimate_resource_cost, estimate_all
from bucksawz.schema.infracost import Resource, CostComponent


# ── Helpers ─────────────────────────────────────────────────────────────────


def _make_resource(
    name: str,
    resource_type: str,
    components: list[CostComponent],
    monthly_cost: float | None = None,
) -> Resource:
    return Resource(
        name=name,
        resource_type=resource_type,
        tags={},
        monthly_cost=monthly_cost,
        hourly_cost=None,
        cost_components=components,
        sub_resources=[],
    )


def _usage_comp(name: str, unit: str, price: float) -> CostComponent:
    return CostComponent(
        name=name, unit=unit,
        hourly_quantity=None, monthly_quantity=None,
        price=price, hourly_cost=None, monthly_cost=None,
        usage_based=True,
    )


def _fixed_comp(name: str, unit: str, price: float, monthly_cost: float) -> CostComponent:
    return CostComponent(
        name=name, unit=unit,
        hourly_quantity=None, monthly_quantity=None,
        price=price, hourly_cost=None, monthly_cost=monthly_cost,
        usage_based=False,
    )


# ── Price DB tests ───────────────────────────────────────────────────────────


@pytest.fixture
def tmp_db(tmp_path) -> Path:
    return tmp_path / "test_prices.db"


def test_db_upsert_and_get(tmp_db):
    price_db.upsert("AmazonECS", "us-east-1", "fargate:vcpu", "vCPU-Hours", 0.04048, db=tmp_db)
    row = price_db.get_price("AmazonECS", "us-east-1", "fargate:vcpu", db=tmp_db)
    assert row is not None
    assert row["price_usd"] == pytest.approx(0.04048)
    assert row["unit"] == "vCPU-Hours"


def test_db_upsert_overwrites(tmp_db):
    price_db.upsert("AmazonECS", "us-east-1", "fargate:vcpu", "vCPU-Hours", 0.04048, db=tmp_db)
    price_db.upsert("AmazonECS", "us-east-1", "fargate:vcpu", "vCPU-Hours", 0.05000, db=tmp_db)
    row = price_db.get_price("AmazonECS", "us-east-1", "fargate:vcpu", db=tmp_db)
    assert row["price_usd"] == pytest.approx(0.05000)


def test_db_get_missing_returns_none(tmp_db):
    assert price_db.get_price("AmazonECS", "us-east-1", "nonexistent", db=tmp_db) is None


def test_db_get_all(tmp_db):
    price_db.upsert("AmazonECS", "us-east-1", "fargate:vcpu", "vCPU-Hours", 0.04048, db=tmp_db)
    price_db.upsert("AmazonECS", "us-east-1", "fargate:memory", "GB-Hours", 0.004445, db=tmp_db)
    price_db.upsert("AWSLambda", "us-east-1", "lambda:requests", "Requests", 0.0000002, db=tmp_db)
    rows = price_db.get_all("AmazonECS", "us-east-1", db=tmp_db)
    assert len(rows) == 2
    keys = {r["price_key"] for r in rows}
    assert keys == {"fargate:vcpu", "fargate:memory"}


def test_db_count(tmp_db):
    assert price_db.count(db=tmp_db) == 0
    price_db.upsert("AmazonECS", "us-east-1", "fargate:vcpu", "vCPU-Hours", 0.04048, db=tmp_db)
    price_db.upsert("AWSLambda", "us-east-1", "lambda:requests", "Requests", 2e-7, db=tmp_db)
    assert price_db.count(db=tmp_db) == 2


def test_db_service_summary(tmp_db):
    price_db.upsert("AmazonECS", "us-east-1", "fargate:vcpu", "vCPU-Hours", 0.04048, db=tmp_db)
    price_db.upsert("AmazonECS", "us-east-1", "fargate:memory", "GB-Hours", 0.004445, db=tmp_db)
    price_db.upsert("AWSLambda", "eu-west-1", "lambda:requests", "Requests", 2e-7, db=tmp_db)
    summary = price_db.service_summary(db=tmp_db)
    assert len(summary) == 2
    ecs = next(r for r in summary if r["service"] == "AmazonECS")
    assert ecs["rows"] == 2
    assert ecs["region"] == "us-east-1"


# ── Estimator tests ──────────────────────────────────────────────────────────


def test_estimator_alb_lcu():
    """ALB: average 4.2 LCUs/hr × 730 hr/mo × $0.008/LCU = $24.53/mo"""
    resource = _make_resource(
        "aws_lb.main", "aws_lb",
        [_usage_comp("Load balancer capacity units", "LCU", 0.008)],
    )
    cw = {"ConsumedLCUs": 4.2, "unit": "LCU"}
    est = estimate_resource_cost(resource, cw, lookback_days=90)
    assert est == pytest.approx(4.2 * 730 * 0.008, rel=1e-4)


def test_estimator_sqs_requests():
    """SQS: 3M requests over 90 days → 1M/month × $0.40/M = $0.40/mo"""
    resource = _make_resource(
        "aws_sqs_queue.jobs", "aws_sqs_queue",
        [_usage_comp("Requests", "1M requests", 0.40)],
    )
    cw = {"Requests": 3.0, "unit": "1M requests"}  # 3M total over 90 days
    est = estimate_resource_cost(resource, cw, lookback_days=90)
    assert est == pytest.approx(0.40, rel=1e-4)  # 3.0 / 3 months × $0.40


def test_estimator_lambda_invocations():
    """Lambda: 6M invocations over 90 days → 2M/month × $0.20/M = $0.40/mo"""
    resource = _make_resource(
        "aws_lambda_function.processor", "aws_lambda_function",
        [_usage_comp("Requests", "1M requests", 0.20)],
    )
    cw = {"Invocations": 6.0, "unit": "1M requests"}
    est = estimate_resource_cost(resource, cw, lookback_days=90)
    assert est == pytest.approx(0.40, rel=1e-4)


def test_estimator_no_actuals_returns_none():
    resource = _make_resource(
        "aws_lb.main", "aws_lb",
        [_usage_comp("Load balancer capacity units", "LCU", 0.008)],
    )
    assert estimate_resource_cost(resource, {}, lookback_days=90) is None


def test_estimator_no_usage_based_returns_none():
    """Fixed-cost-only resource should return None."""
    resource = _make_resource(
        "aws_instance.bastion", "aws_instance",
        [_fixed_comp("Instance usage", "hours", 0.0104, 7.59)],
        monthly_cost=7.59,
    )
    cw = {"ConsumedLCUs": 4.2}
    assert estimate_resource_cost(resource, cw, lookback_days=90) is None


def test_estimator_missing_price_returns_none():
    """Usage-based component with no price should not be estimated."""
    comp = CostComponent(
        name="Load balancer capacity units", unit="LCU",
        hourly_quantity=None, monthly_quantity=None,
        price=None, hourly_cost=None, monthly_cost=None,
        usage_based=True,
    )
    resource = _make_resource("aws_lb.main", "aws_lb", [comp])
    cw = {"ConsumedLCUs": 4.2}
    assert estimate_resource_cost(resource, cw, lookback_days=90) is None


def test_estimator_mixed_components():
    """Fixed + usage-based: only usage-based component contributes to estimate."""
    resource = _make_resource(
        "aws_lb.main", "aws_lb",
        [
            _fixed_comp("Application load balancer", "hours", 0.025205, 18.40),
            _usage_comp("Load balancer capacity units", "LCU", 0.008),
        ],
        monthly_cost=18.40,
    )
    cw = {"ConsumedLCUs": 5.0}
    est = estimate_resource_cost(resource, cw, lookback_days=90)
    assert est == pytest.approx(5.0 * 730 * 0.008, rel=1e-4)


def test_estimate_all():
    """estimate_all returns dict keyed by resource name."""
    alb = _make_resource(
        "aws_lb.main", "aws_lb",
        [_usage_comp("Load balancer capacity units", "LCU", 0.008)],
    )
    sqs = _make_resource(
        "aws_sqs_queue.jobs", "aws_sqs_queue",
        [_usage_comp("Requests", "1M requests", 0.40)],
    )
    fixed = _make_resource(
        "aws_instance.bastion", "aws_instance",
        [_fixed_comp("Instance usage", "hours", 0.0104, 7.59)],
        monthly_cost=7.59,
    )
    cw_actuals = {
        "aws_lb.main": {"ConsumedLCUs": 2.0, "unit": "LCU"},
        "aws_sqs_queue.jobs": {"Requests": 1.5, "unit": "1M requests"},
    }
    results = estimate_all([alb, sqs, fixed], cw_actuals, lookback_days=90)
    assert "aws_lb.main" in results
    assert "aws_sqs_queue.jobs" in results
    assert "aws_instance.bastion" not in results
    assert results["aws_lb.main"] == pytest.approx(2.0 * 730 * 0.008, rel=1e-4)
