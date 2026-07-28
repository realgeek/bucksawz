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
    price_db.upsert("AmazonEC2", "us-east-1", "ebs:storage:gp3", "GB-Mo", 0.08, db=db)
    price_db.upsert("AmazonEC2", "us-east-1", "ebs:storage:gp2", "GB-Mo", 0.10, db=db)
    price_db.upsert("AmazonEC2", "us-east-1", "ebs:storage:io1", "GB-Mo", 0.125, db=db)
    price_db.upsert("AmazonEC2", "us-east-1", "ebs:storage:io2", "GB-Mo", 0.125, db=db)
    price_db.upsert("AmazonEC2", "us-east-1", "ebs:iops:gp3", "IOPS-Mo", 0.005, db=db)
    price_db.upsert("AmazonEC2", "us-east-1", "ebs:iops:io1", "IOPS-Mo", 0.065, db=db)
    price_db.upsert("AmazonEC2", "us-east-1", "ebs:iops:io2:tier1", "IOPS-Mo", 0.065, db=db)
    price_db.upsert("AmazonEC2", "us-east-1", "ebs:iops:io2:tier2", "IOPS-Mo", 0.0455, db=db)
    price_db.upsert("AmazonEC2", "us-east-1", "ebs:iops:io2:tier3", "IOPS-Mo", 0.03185, db=db)
    price_db.upsert("AmazonEC2", "us-east-1", "ebs:throughput:gp3", "MiBps-Mo", 0.04, db=db)
    price_db.upsert("AWSSecretsManager", "us-east-1", "secretsmanager:secret", "Secrets", 0.40, db=db)
    price_db.upsert("AWSSecretsManager", "us-east-1", "secretsmanager:requests", "API Requests", 5e-6, db=db)
    price_db.upsert("AmazonRoute53", "us-east-1", "route53:hostedzone", "HostedZone", 0.50, db=db)
    price_db.upsert("AmazonRoute53", "us-east-1", "route53:queries", "Queries", 4e-7, db=db)
    price_db.upsert("awskms", "us-east-1", "kms:key", "Keys", 1.0, db=db)
    price_db.upsert("awskms", "us-east-1", "kms:requests", "Requests", 3e-6, db=db)
    price_db.upsert("awswaf", "us-east-1", "waf:webacl", "Month", 5.0, db=db)
    price_db.upsert("awswaf", "us-east-1", "waf:rule", "Month", 1.0, db=db)
    price_db.upsert("awswaf", "us-east-1", "waf:requests", "Request", 6e-7, db=db)
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


# ── Secrets Manager ───────────────────────────────────────────────────────────


def test_secretsmanager_secret_flat_monthly_cost(tmp_db):
    """Unlike S3/SQS, a secret's price doesn't depend on its config or usage."""
    tf = _tf("aws_secretsmanager_secret", {})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost == pytest.approx(0.40)


def test_secretsmanager_secret_has_fixed_and_usage_based_components(tmp_db):
    tf = _tf("aws_secretsmanager_secret", {})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    fixed, requests = resource.cost_components
    assert not fixed.usage_based
    assert fixed.monthly_cost == pytest.approx(0.40)
    assert requests.usage_based
    assert requests.monthly_cost is None
    assert requests.unit == "1M requests"
    assert requests.price == pytest.approx(5.0)


def test_secretsmanager_secret_unpriced_without_cached_price(empty_db):
    tf = _tf("aws_secretsmanager_secret", {})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


# ── Route 53 ─────────────────────────────────────────────────────────────────


def test_route53_zone_flat_monthly_cost(tmp_db):
    """Like a Secrets Manager secret, a hosted zone's base price is known from
    config alone; query volume is not."""
    tf = _tf("aws_route53_zone", {"name": "example.com"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost == pytest.approx(0.50)
    fixed, queries = resource.cost_components
    assert not fixed.usage_based
    assert queries.usage_based
    assert queries.monthly_cost is None
    assert queries.price == pytest.approx(0.40)


def test_route53_zone_unpriced_without_cached_price(empty_db):
    tf = _tf("aws_route53_zone", {"name": "example.com"})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


# ── KMS ──────────────────────────────────────────────────────────────────────


def test_kms_key_flat_monthly_cost(tmp_db):
    tf = _tf("aws_kms_key", {"description": "app secrets"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost == pytest.approx(1.0)
    fixed, requests = resource.cost_components
    assert not fixed.usage_based
    assert requests.usage_based
    assert requests.price == pytest.approx(3.0)


def test_kms_key_unpriced_without_cached_price(empty_db):
    tf = _tf("aws_kms_key", {})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


# ── WAF ──────────────────────────────────────────────────────────────────────


def test_waf_web_acl_with_no_rules(tmp_db):
    tf = _tf("aws_wafv2_web_acl", {"name": "api-waf"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost == pytest.approx(5.0)
    names = [c.name for c in resource.cost_components]
    assert names == ["Web ACL", "Requests"]


def test_waf_web_acl_rule_count_folded_into_total(tmp_db):
    tf = _tf("aws_wafv2_web_acl", {
        "name": "api-waf",
        "rule": [{"name": "rate-limit"}, {"name": "sql-injection"}, {"name": "geo-block"}],
    })
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(5.0 + 3 * 1.0)
    rules_comp = next(c for c in resource.cost_components if c.name.startswith("Rules"))
    assert rules_comp.monthly_quantity == 3.0
    assert rules_comp.monthly_cost == pytest.approx(3.0)


def test_waf_web_acl_bare_dict_single_rule(tmp_db):
    """A single `rule` block can show up as a bare dict rather than a list of one."""
    tf = _tf("aws_wafv2_web_acl", {"name": "api-waf", "rule": {"name": "rate-limit"}})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(5.0 + 1.0)


def test_waf_web_acl_request_component_is_usage_based(tmp_db):
    tf = _tf("aws_wafv2_web_acl", {"name": "api-waf"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    requests_comp = next(c for c in resource.cost_components if c.name == "Requests")
    assert requests_comp.usage_based
    assert requests_comp.monthly_cost is None
    assert requests_comp.price == pytest.approx(0.60)


def test_waf_web_acl_unpriced_without_cached_price(empty_db):
    tf = _tf("aws_wafv2_web_acl", {"name": "api-waf"})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


# ── EBS: standalone aws_ebs_volume ───────────────────────────────────────────


def test_ebs_volume_gp3_storage_only(tmp_db):
    tf = _tf("aws_ebs_volume", {"type": "gp3", "size": 100})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(100 * 0.08)
    [comp] = resource.cost_components
    assert comp.name == "Storage (gp3, 100 GB)"


def test_ebs_volume_defaults_to_gp2_when_type_missing(tmp_db):
    tf = _tf("aws_ebs_volume", {"size": 50})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(50 * 0.10)


def test_ebs_volume_missing_size_is_unpriced(tmp_db):
    tf = _tf("aws_ebs_volume", {"type": "gp3"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.no_price


def test_ebs_volume_uncached_type_is_unpriced(empty_db):
    tf = _tf("aws_ebs_volume", {"type": "gp3", "size": 100})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


def test_ebs_gp3_iops_below_baseline_is_free(tmp_db):
    """3,000 IOPS is included in gp3's storage price."""
    tf = _tf("aws_ebs_volume", {"type": "gp3", "size": 100, "iops": 3000})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(100 * 0.08)
    assert len(resource.cost_components) == 1


def test_ebs_gp3_iops_above_baseline_billed_on_the_excess(tmp_db):
    tf = _tf("aws_ebs_volume", {"type": "gp3", "size": 100, "iops": 4000})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    expected = (100 * 0.08) + (1000 * 0.005)
    assert resource.monthly_cost == pytest.approx(expected)


def test_ebs_gp3_throughput_below_baseline_is_free(tmp_db):
    """125 MiB/s is included in gp3's storage price."""
    tf = _tf("aws_ebs_volume", {"type": "gp3", "size": 100, "throughput": 125})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(100 * 0.08)


def test_ebs_gp3_throughput_above_baseline_billed_on_the_excess(tmp_db):
    tf = _tf("aws_ebs_volume", {"type": "gp3", "size": 100, "throughput": 500})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    expected = (100 * 0.08) + (375 * 0.04)
    assert resource.monthly_cost == pytest.approx(expected)


def test_ebs_gp3_iops_and_throughput_both_billed(tmp_db):
    tf = _tf("aws_ebs_volume", {"type": "gp3", "size": 100, "iops": 5000, "throughput": 250})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    expected = (100 * 0.08) + (2000 * 0.005) + (125 * 0.04)
    assert resource.monthly_cost == pytest.approx(expected)
    assert len(resource.cost_components) == 3


def test_ebs_io1_iops_has_no_free_tier(tmp_db):
    tf = _tf("aws_ebs_volume", {"type": "io1", "size": 100, "iops": 1000})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    expected = (100 * 0.125) + (1000 * 0.065)
    assert resource.monthly_cost == pytest.approx(expected)


def test_ebs_io2_iops_within_first_tier(tmp_db):
    tf = _tf("aws_ebs_volume", {"type": "io2", "size": 100, "iops": 10000})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    expected = (100 * 0.125) + (10000 * 0.065)
    assert resource.monthly_cost == pytest.approx(expected)


def test_ebs_io2_iops_blends_across_tier_boundaries(tmp_db):
    """40,000 IOPS = 32,000 @ tier1 + 8,000 @ tier2 — not 40,000 at either rate."""
    tf = _tf("aws_ebs_volume", {"type": "io2", "size": 100, "iops": 40000})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    expected = (100 * 0.125) + (32000 * 0.065) + (8000 * 0.0455)
    assert resource.monthly_cost == pytest.approx(expected)


def test_ebs_io2_iops_reaches_third_tier(tmp_db):
    tf = _tf("aws_ebs_volume", {"type": "io2", "size": 100, "iops": 70000})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    expected = (100 * 0.125) + (32000 * 0.065) + (32000 * 0.0455) + (6000 * 0.03185)
    assert resource.monthly_cost == pytest.approx(expected)


def test_ebs_st1_has_no_iops_component(tmp_db):
    """st1/sc1/standard don't take a separate IOPS charge even if given one."""
    price_db.upsert("AmazonEC2", "us-east-1", "ebs:storage:st1", "GB-Mo", 0.045, db=tmp_db)
    tf = _tf("aws_ebs_volume", {"type": "st1", "size": 500, "iops": 500})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(500 * 0.045)
    assert len(resource.cost_components) == 1


# ── EBS: attached to aws_instance / aws_launch_template ──────────────────────


def test_instance_root_volume_adds_to_total(tmp_db):
    tf = _tf("aws_instance", {
        "instance_type": "t3.micro",
        "root_block_device": [{"volume_type": "gp3", "volume_size": 20}],
    })
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    expected = (0.0104 * 730) + (20 * 0.08)
    assert resource.monthly_cost == pytest.approx(expected)
    [root] = resource.sub_resources
    assert root.monthly_cost == pytest.approx(20 * 0.08)


def test_instance_root_block_device_as_bare_dict(tmp_db):
    """Some state exports give root_block_device as a single dict, not a list."""
    tf = _tf("aws_instance", {
        "instance_type": "t3.micro",
        "root_block_device": {"volume_type": "gp3", "volume_size": 20},
    })
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert len(resource.sub_resources) == 1


def test_instance_additional_ebs_block_devices(tmp_db):
    tf = _tf("aws_instance", {
        "instance_type": "t3.micro",
        "root_block_device": [{"volume_type": "gp3", "volume_size": 20}],
        "ebs_block_device": [
            {"device_name": "/dev/sdf", "volume_type": "gp2", "volume_size": 100},
            {"device_name": "/dev/sdg", "volume_type": "gp2", "volume_size": 200},
        ],
    })
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    expected = (0.0104 * 730) + (20 * 0.08) + (100 * 0.10) + (200 * 0.10)
    assert resource.monthly_cost == pytest.approx(expected)
    assert len(resource.sub_resources) == 3
    names = [s.name for s in resource.sub_resources]
    assert any("/dev/sdf" in n for n in names)
    assert any("/dev/sdg" in n for n in names)


def test_instance_with_no_block_devices_is_unaffected(tmp_db):
    tf = _tf("aws_instance", {"instance_type": "t3.micro"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(0.0104 * 730)
    assert resource.sub_resources == []


def test_launch_template_block_device_mappings(tmp_db):
    tf = _tf("aws_launch_template", {
        "instance_type": "t3.micro",
        "block_device_mappings": [
            {"device_name": "/dev/xvda", "ebs": [{"volume_type": "gp3", "volume_size": 30}]},
        ],
    })
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    expected = (0.0104 * 730) + (30 * 0.08)
    assert resource.monthly_cost == pytest.approx(expected)


def test_launch_template_no_device_mapping_is_skipped(tmp_db):
    """A mapping with no `ebs` block (ephemeral / no_device) isn't an EBS volume."""
    tf = _tf("aws_launch_template", {
        "instance_type": "t3.micro",
        "block_device_mappings": [
            {"device_name": "ephemeral0", "virtual_name": "ephemeral0", "ebs": []},
        ],
    })
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.sub_resources == []


def test_instance_unpriced_block_device_still_visible(tmp_db):
    """An EBS type with no cached price shows up as no_price, not silently dropped."""
    tf = _tf("aws_instance", {
        "instance_type": "t3.micro",
        "root_block_device": [{"volume_type": "sc1", "volume_size": 20}],
    })
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(0.0104 * 730)  # instance cost unaffected
    [root] = resource.sub_resources
    assert root.no_price


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
