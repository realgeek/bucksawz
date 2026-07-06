"""Tests for pricing terraform resource configs directly against the SQLite price cache."""
import pytest
from pathlib import Path
from bucksawz.pricing import db as price_db
from bucksawz.pricing.pricer import build_output, price_resources
from bucksawz.pricing.tf_state import TFResource


@pytest.fixture
def tmp_db(tmp_path) -> Path:
    db = tmp_path / "test_prices.db"
    price_db.upsert("AmazonEC2", "us-east-1", "ec2:t3.micro:linux:shared", "Hrs", 0.0104, db=db)
    price_db.upsert("AmazonRDS", "us-east-1", "rds:db.t3.medium:PostgreSQL:Single-AZ", "Hrs", 0.068, db=db)
    price_db.upsert("AmazonECS", "us-east-1", "fargate:vcpu", "vCPU-Hours", 0.04048, db=db)
    price_db.upsert("AmazonECS", "us-east-1", "fargate:memory", "GB-Hours", 0.004445, db=db)
    price_db.upsert("AmazonECS", "us-east-1", "fargate:vcpu:arm", "vCPU-Hours", 0.03238, db=db)
    price_db.upsert("AmazonECS", "us-east-1", "fargate:memory:arm", "GB-Hours", 0.003556, db=db)
    price_db.upsert("AWSLambda", "us-east-1", "lambda:requests", "Requests", 2e-7, db=db)
    price_db.upsert("AWSLambda", "us-east-1", "lambda:duration:x86_64", "GB-Seconds", 1.6667e-5, db=db)
    return db


def _tf(type_, values, address=None):
    return TFResource(
        address=address or f"{type_}.thing",
        type=type_,
        name="thing",
        provider_name="registry.terraform.io/hashicorp/aws",
        values=values,
    )


def test_ec2_instance_priced(tmp_db):
    tf = _tf("aws_instance", {"instance_type": "t3.micro"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost == pytest.approx(0.0104 * 730)


def test_ec2_missing_instance_type_unpriced(tmp_db):
    tf = _tf("aws_instance", {})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert not resource.is_supported
    assert resource.no_price


def test_ec2_no_price_data_for_region(tmp_db):
    tf = _tf("aws_instance", {"instance_type": "m5.24xlarge"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.no_price


def test_rds_instance_priced(tmp_db):
    tf = _tf("aws_db_instance", {"instance_class": "db.t3.medium", "engine": "postgres", "multi_az": False})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(0.068 * 730)


def test_rds_unsupported_engine_unpriced(tmp_db):
    tf = _tf("aws_db_instance", {"instance_class": "db.t3.medium", "engine": "oracle-se2"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.no_price


def test_lb_priced_with_usage_based_lcu(tmp_db):
    tf = _tf("aws_lb", {"load_balancer_type": "application"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(0.0225 * 730)
    lcu = next(c for c in resource.cost_components if c.usage_based)
    assert lcu.unit == "LCU"
    assert lcu.monthly_cost is None


def test_ecs_fargate_x86_priced(tmp_db):
    tf = _tf("aws_ecs_task_definition", {
        "requires_compatibilities": ["FARGATE"],
        "cpu": "512",
        "memory": "1024",
        "runtime_platform": [{"cpu_architecture": "X86_64"}],
    })
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    expected = (0.5 * 730 * 0.04048) + (1.0 * 730 * 0.004445)
    assert resource.monthly_cost == pytest.approx(expected)


def test_ecs_fargate_arm_uses_arm_prices(tmp_db):
    tf = _tf("aws_ecs_task_definition", {
        "requires_compatibilities": ["FARGATE"],
        "cpu": "512",
        "memory": "1024",
        "runtime_platform": [{"cpu_architecture": "ARM64"}],
    })
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    expected = (0.5 * 730 * 0.03238) + (1.0 * 730 * 0.003556)
    assert resource.monthly_cost == pytest.approx(expected)


def test_ecs_non_fargate_skipped(tmp_db):
    tf = _tf("aws_ecs_task_definition", {
        "requires_compatibilities": ["EC2"],
        "cpu": "512",
        "memory": "1024",
    })
    assert price_resources([tf], "us-east-1", db=tmp_db) == []


def test_lambda_priced_as_usage_based(tmp_db):
    tf = _tf("aws_lambda_function", {"memory_size": 256, "architectures": ["x86_64"]})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost is None
    assert len(resource.cost_components) == 2
    assert all(c.usage_based for c in resource.cost_components)
    duration = next(c for c in resource.cost_components if "Duration" in c.name)
    assert duration.price == pytest.approx(1.6667e-5)


def test_unsupported_resource_type_skipped(tmp_db):
    tf = _tf("aws_s3_bucket", {})
    assert price_resources([tf], "us-east-1", db=tmp_db) == []


def test_build_output_totals(tmp_db):
    tf_ec2 = _tf("aws_instance", {"instance_type": "t3.micro"})
    tf_bad = _tf("aws_instance", {})
    resources = price_resources([tf_ec2, tf_bad], "us-east-1", db=tmp_db)
    output = build_output(resources, "us-east-1")
    assert output.total_monthly_cost == pytest.approx(0.0104 * 730)
    assert output.summary["totalDetectedResources"] == 2
    assert output.summary["totalSupportedResources"] == 1
    assert output.summary["totalNoPriceResources"] == 1
    assert len(output.projects) == 1
    assert output.projects[0].breakdown.resources == resources
