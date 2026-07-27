"""Tests for pricing terraform resource configs directly against the SQLite price cache."""
import pytest
from pathlib import Path
from bucksawz.pricing import db as price_db
from bucksawz.pricing.estimator import estimate_resource_cost
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
    price_db.upsert("AWSELB", "us-east-1", "elb:hourly:application", "Hrs", 0.0243, db=db)
    price_db.upsert("AWSELB", "us-east-1", "elb:lcu:application", "LCU-Hrs", 0.008, db=db)
    price_db.upsert("AWSELB", "us-east-1", "elb:hourly:classic", "Hrs", 0.027, db=db)
    price_db.upsert("AWSELB", "us-east-1", "elb:data:classic", "GB", 0.008, db=db)
    price_db.upsert("AmazonElastiCache", "us-east-1", "elasticache:cache.t3.micro:redis", "Hrs", 0.017, db=db)
    price_db.upsert("AmazonS3", "us-east-1", "s3:storage:standard", "GB-Mo", 0.023, db=db)
    price_db.upsert("AWSQueueService", "us-east-1", "sqs:requests:standard", "Requests", 4e-7, db=db)
    price_db.upsert("AWSQueueService", "us-east-1", "sqs:requests:fifo", "Requests", 5e-7, db=db)
    return db


@pytest.fixture
def empty_db(tmp_path) -> Path:
    """A price cache with no rows — exercises the no-price / fallback paths."""
    return tmp_path / "empty_prices.db"


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
    assert resource.monthly_cost == pytest.approx(0.0243 * 730)  # cached rate, not the fallback
    lcu = next(c for c in resource.cost_components if c.usage_based)
    assert lcu.unit == "LCU"
    assert lcu.price == pytest.approx(0.008)
    assert lcu.monthly_cost is None


def test_lb_falls_back_to_flat_rate_without_cached_price(empty_db):
    """`prices update --services ELB` not run yet: approximate rather than drop the cost."""
    tf = _tf("aws_lb", {"load_balancer_type": "application"})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.monthly_cost == pytest.approx(0.0225 * 730)
    lcu = next(c for c in resource.cost_components if c.usage_based)
    assert lcu.price == pytest.approx(0.008)


def test_lb_unknown_type_treated_as_application(tmp_db):
    tf = _tf("aws_lb", {"load_balancer_type": "quantum"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(0.0243 * 730)


def test_classic_elb_bills_data_processed_not_lcus(tmp_db):
    tf = _tf("aws_elb", {})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(0.027 * 730)
    variable = next(c for c in resource.cost_components if c.usage_based)
    assert variable.unit == "GB"
    assert variable.price == pytest.approx(0.008)


def test_alb_alias_is_priced(tmp_db):
    tf = _tf("aws_alb", {"load_balancer_type": "application"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(0.0243 * 730)


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


def test_lambda_request_price_normalised_to_millions(tmp_db):
    """
    The Pricing API quotes SQS/Lambda requests per single request, but the unit
    reported is "1M requests" — and estimator.py multiplies millions of requests
    by that price, so the price must be scaled to match or the estimate is 1e6 too low.
    """
    tf = _tf("aws_lambda_function", {"architectures": ["x86_64"]})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    requests = next(c for c in resource.cost_components if c.name == "Requests")
    assert requests.unit == "1M requests"
    assert requests.price == pytest.approx(0.20)


def test_lambda_estimate_matches_cloudwatch_actuals(tmp_db):
    """End-to-end unit check on the pricer → estimator handoff."""
    tf = _tf("aws_lambda_function", {"architectures": ["x86_64"]})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    # 6M invocations over 90 days → 2M/month × $0.20/M = $0.40/mo
    est = estimate_resource_cost(resource, {"Invocations": 6.0}, lookback_days=90)
    assert est == pytest.approx(0.40, rel=1e-4)


# ── ElastiCache ──────────────────────────────────────────────────────────────


def test_elasticache_cluster_priced_per_node(tmp_db):
    tf = _tf("aws_elasticache_cluster", {
        "node_type": "cache.t3.micro", "engine": "redis", "num_cache_nodes": 2,
    })
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(0.017 * 2 * 730)
    assert resource.cost_components[0].hourly_quantity == pytest.approx(2.0)


def test_elasticache_defaults_to_redis_and_one_node(tmp_db):
    tf = _tf("aws_elasticache_cluster", {"node_type": "cache.t3.micro"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(0.017 * 730)


def test_elasticache_replication_group_uses_cluster_count(tmp_db):
    tf = _tf("aws_elasticache_replication_group", {
        "node_type": "cache.t3.micro", "num_cache_clusters": 3,
    })
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(0.017 * 3 * 730)


def test_elasticache_cluster_mode_counts_shards_and_replicas(tmp_db):
    """2 shards × (1 primary + 1 replica) = 4 billed nodes."""
    tf = _tf("aws_elasticache_replication_group", {
        "node_type": "cache.t3.micro", "num_node_groups": 2, "replicas_per_node_group": 1,
    })
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(0.017 * 4 * 730)


def test_elasticache_missing_node_type_unpriced(tmp_db):
    tf = _tf("aws_elasticache_cluster", {"engine": "redis"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.no_price


def test_elasticache_uncached_engine_unpriced(tmp_db):
    tf = _tf("aws_elasticache_cluster", {"node_type": "cache.t3.micro", "engine": "memcached"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.no_price


# ── S3 / SQS ─────────────────────────────────────────────────────────────────


def test_s3_bucket_priced_as_usage_based_storage(tmp_db):
    """Bucket size isn't in the terraform config, so storage stays usage-based."""
    tf = _tf("aws_s3_bucket", {})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost is None
    [comp] = resource.cost_components
    assert comp.usage_based
    assert comp.unit == "GB-months"
    assert comp.price == pytest.approx(0.023)


def test_s3_bucket_unpriced_without_cached_price(empty_db):
    tf = _tf("aws_s3_bucket", {})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


def test_sqs_standard_queue_priced_per_million(tmp_db):
    tf = _tf("aws_sqs_queue", {})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    [comp] = resource.cost_components
    assert comp.unit == "1M requests"
    assert comp.price == pytest.approx(0.40)


def test_sqs_fifo_queue_uses_fifo_price(tmp_db):
    tf = _tf("aws_sqs_queue", {"fifo_queue": True})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.cost_components[0].price == pytest.approx(0.50)


def test_unsupported_resource_type_skipped(tmp_db):
    tf = _tf("aws_iam_role", {})
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
