"""Tests for CloudWatch metric enrichment: dimension-building and unit
conversion for the resource-type branches in enrich_with_cloudwatch."""
import pytest
from bucksawz.aws import cloudwatch
from bucksawz.schema.infracost import CostComponent, Resource


def _usage_comp(name, unit, price):
    return CostComponent(
        name=name, unit=unit, hourly_quantity=None, monthly_quantity=None,
        price=price, hourly_cost=None, monthly_cost=None, usage_based=True,
    )


def _resource(name, resource_type, components):
    return Resource(
        name=name, resource_type=resource_type, tags={}, monthly_cost=None,
        hourly_cost=None, cost_components=components, sub_resources=[],
    )


class _FakeCW:
    """Stands in for a boto3 cloudwatch client; records the request it received."""

    def __init__(self, value):
        self.value = value
        self.last_call = None

    def get_metric_statistics(self, **kwargs):
        self.last_call = kwargs
        if self.value is None:
            return {"Datapoints": []}
        return {"Datapoints": [{"Average": self.value, "Sum": self.value}]}


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    monkeypatch.setattr(cloudwatch, "cache_get", lambda *a, **k: None)
    monkeypatch.setattr(cloudwatch, "cache_put", lambda *a, **k: None)


def _install(monkeypatch, value):
    fake = _FakeCW(value)
    monkeypatch.setattr(cloudwatch, "_cw_client", lambda profile, region: fake)
    return fake


def test_log_group_ingestion_queried_with_log_group_name_dimension(monkeypatch):
    fake = _install(monkeypatch, 1024.0 ** 3 * 5)  # 5 GB total ingested
    resource = _resource(
        "aws_cloudwatch_log_group.app", "aws_cloudwatch_log_group",
        [_usage_comp("Data ingested", "GB", 0.50)],
    )
    result = cloudwatch.enrich_with_cloudwatch([resource], None, "us-east-1", lookback_days=90, ttl_days=7)
    assert fake.last_call["Namespace"] == "AWS/Logs"
    assert fake.last_call["MetricName"] == "IncomingBytes"
    assert fake.last_call["Dimensions"] == [{"Name": "LogGroupName", "Value": "app"}]
    assert result["aws_cloudwatch_log_group.app"]["IngestedBytes"] == pytest.approx(1024.0 ** 3 * 5)


def test_s3_bucket_storage_queried_with_bucket_name_and_storage_type(monkeypatch):
    fake = _install(monkeypatch, 1024.0 ** 3 * 200)  # 200 GB average size
    resource = _resource(
        "aws_s3_bucket.data", "aws_s3_bucket",
        [_usage_comp("Standard storage", "GB-months", 0.023)],
    )
    result = cloudwatch.enrich_with_cloudwatch([resource], None, "us-east-1", lookback_days=90, ttl_days=7)
    assert fake.last_call["Namespace"] == "AWS/S3"
    assert fake.last_call["MetricName"] == "BucketSizeBytes"
    assert fake.last_call["Dimensions"] == [
        {"Name": "BucketName", "Value": "data"},
        {"Name": "StorageType", "Value": "StandardStorage"},
    ]
    assert result["aws_s3_bucket.data"]["StorageGB"] == pytest.approx(200.0)


def test_s3_bucket_no_datapoints_omits_resource(monkeypatch):
    _install(monkeypatch, None)
    resource = _resource(
        "aws_s3_bucket.data", "aws_s3_bucket",
        [_usage_comp("Standard storage", "GB-months", 0.023)],
    )
    result = cloudwatch.enrich_with_cloudwatch([resource], None, "us-east-1", lookback_days=90, ttl_days=7)
    assert result == {}


def test_resource_without_usage_based_components_skipped(monkeypatch):
    fake = _install(monkeypatch, 999.0)
    resource = _resource(
        "aws_s3_bucket.data", "aws_s3_bucket",
        [CostComponent(
            name="fixed", unit="months", hourly_quantity=None, monthly_quantity=1.0,
            price=1.0, hourly_cost=None, monthly_cost=1.0, usage_based=False,
        )],
    )
    result = cloudwatch.enrich_with_cloudwatch([resource], None, "us-east-1", lookback_days=90, ttl_days=7)
    assert result == {}
    assert fake.last_call is None
