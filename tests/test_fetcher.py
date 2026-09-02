"""
Tests for the AWS Pricing API fetchers.

The Pricing API does most of the coarse filtering server-side, so these tests
feed each fetcher a product list that has *already passed* the server-side
filters and assert on the client-side filtering — which usagetypes are kept,
which price_key they land under, and which adjacent line items get rejected.
Those exclusions are the fragile part: several contaminants share a usagetype
suffix with the real regional price and would silently overwrite it.
"""
import json
import pytest
from pathlib import Path
from bucksawz.pricing import db as price_db
from bucksawz.pricing import fetcher


# ── Fakes ────────────────────────────────────────────────────────────────────


class _FakePaginator:
    def __init__(self, client):
        self._client = client

    def paginate(self, ServiceCode, Filters):
        self._client.service_code = ServiceCode
        self._client.filters = Filters
        # One page is enough; _iter_products' paging is boto3's concern.
        yield {"PriceList": [json.dumps(p) for p in self._client.products]}


class _FakePricing:
    """Stands in for a boto3 pricing client, recording the request it received."""

    def __init__(self, products):
        self.products = products
        self.service_code = None
        self.filters = None

    def get_paginator(self, name):
        assert name == "get_products"
        return _FakePaginator(self)


@pytest.fixture
def tmp_db(tmp_path) -> Path:
    return tmp_path / "test_prices.db"


@pytest.fixture
def fake_pricing(monkeypatch):
    """Returns a setter: call with a product list to install the fake client."""
    holder = {}

    def _install(products):
        client = _FakePricing(products)
        holder["client"] = client
        monkeypatch.setattr(fetcher, "_pricing_client", lambda profile=None: client)
        return client

    return _install


def _dim(price, unit="Hrs", desc="", begin_range="0"):
    return {
        "pricePerUnit": {"USD": str(price)},
        "unit": unit,
        "description": desc,
        "beginRange": begin_range,
    }


def _product(family, attrs, dims):
    """dims: list of price dimensions, or a single dimension dict."""
    if isinstance(dims, dict):
        dims = [dims]
    return {
        "product": {"productFamily": family, "attributes": attrs},
        "terms": {"OnDemand": {"offer": {
            "priceDimensions": {f"dim{i}": d for i, d in enumerate(dims)}
        }}},
    }


def _keys(service, region, db) -> dict[str, float]:
    return {r["price_key"]: r["price_usd"] for r in price_db.get_all(service, region, db=db)}


# ── Region display names ─────────────────────────────────────────────────────


def test_region_display_known():
    assert fetcher.region_display("eu-west-2") == "EU (London)"


def test_region_display_unknown_passes_through():
    assert fetcher.region_display("mars-north-1") == "mars-north-1"


# ── Price extraction helpers ─────────────────────────────────────────────────


def test_ondemand_price_skips_zero_dimensions():
    product = _product("Compute", {}, [_dim(0.0), _dim(0.5, unit="Hrs", desc="real")])
    unit, price, desc = fetcher._ondemand_price(product)
    assert (unit, price, desc) == ("Hrs", 0.5, "real")


def test_ondemand_price_none_when_all_free():
    product = _product("Compute", {}, [_dim(0.0), _dim("0.0000000000")])
    assert fetcher._ondemand_price(product) is None


def test_ondemand_price_none_on_malformed_price():
    product = _product("Compute", {}, [_dim("not-a-number")])
    assert fetcher._ondemand_price(product) is None


def test_ondemand_price_none_without_ondemand_terms():
    assert fetcher._ondemand_price({"product": {}, "terms": {}}) is None


def test_first_tier_price_prefers_begin_range_zero():
    """Tiered pricing: the cheaper high-volume tiers must not win."""
    product = _product("Metric", {}, [
        _dim(0.05, begin_range="10000"),
        _dim(0.30, begin_range="0"),
    ])
    _, price, _ = fetcher._first_tier_price(product)
    assert price == pytest.approx(0.30)


def test_first_tier_price_falls_through_free_first_tier():
    """A $0 first tier (free allowance) should not shadow the first paid tier."""
    product = _product("Metric", {}, [
        _dim(0.0, begin_range="0"),
        _dim(0.10, begin_range="1000"),
    ])
    _, price, _ = fetcher._first_tier_price(product)
    assert price == pytest.approx(0.10)


# ── ECS / Fargate ────────────────────────────────────────────────────────────


def test_fetch_fargate_stores_vcpu_and_memory(fake_pricing, tmp_db):
    fake_pricing([
        _product("Compute", {"usagetype": "USE1-Fargate-vCPU-Hours:perCPU"},
                 _dim(0.04048, unit="hours")),
        _product("Compute", {"usagetype": "USE1-Fargate-GB-Hours"},
                 _dim(0.004445, unit="hours")),
    ])
    assert fetcher.fetch_fargate("us-east-1", db=tmp_db) == 2
    assert _keys("AmazonECS", "us-east-1", tmp_db) == {
        "fargate:vcpu": pytest.approx(0.04048),
        "fargate:memory": pytest.approx(0.004445),
    }


def test_fetch_fargate_arm_keyed_separately(fake_pricing, tmp_db):
    fake_pricing([
        _product("Compute", {"usagetype": "USE1-Fargate-ARM-vCPU-Hours:perCPU"}, _dim(0.03238)),
        _product("Compute", {"usagetype": "USE1-Fargate-ARM-GB-Hours"}, _dim(0.003556)),
    ])
    fetcher.fetch_fargate("us-east-1", db=tmp_db)
    assert set(_keys("AmazonECS", "us-east-1", tmp_db)) == {
        "fargate:vcpu:arm", "fargate:memory:arm",
    }


def test_fetch_fargate_excludes_windows_and_ephemeral_storage(fake_pricing, tmp_db):
    fake_pricing([
        _product("Compute", {"usagetype": "USE1-Fargate-Windows-vCPU-Hours:perCPU"}, _dim(0.09148)),
        _product("Compute", {"usagetype": "USE1-Fargate-EphemeralStorage-GB-Hours"}, _dim(0.000111)),
    ])
    assert fetcher.fetch_fargate("us-east-1", db=tmp_db) == 0


def test_fetch_fargate_ignores_non_compute_and_non_fargate(fake_pricing, tmp_db):
    fake_pricing([
        _product("Storage", {"usagetype": "USE1-Fargate-vCPU-Hours:perCPU"}, _dim(1.0)),
        _product("Compute", {"usagetype": "USE1-BoxUsage:t3.micro"}, _dim(1.0)),
    ])
    assert fetcher.fetch_fargate("us-east-1", db=tmp_db) == 0


def test_fetch_fargate_filters_on_region_code(fake_pricing, tmp_db):
    client = fake_pricing([])
    fetcher.fetch_fargate("eu-west-2", db=tmp_db)
    assert client.service_code == "AmazonECS"
    assert {"Type": "TERM_MATCH", "Field": "regionCode", "Value": "eu-west-2"} in client.filters


# ── Lambda ───────────────────────────────────────────────────────────────────


def test_fetch_lambda_requests_and_duration_by_arch(fake_pricing, tmp_db):
    fake_pricing([
        _product("Serverless", {"group": "AWS-Lambda-Requests"}, _dim(2e-7, unit="Requests")),
        _product("Serverless", {"group": "AWS-Lambda-Duration",
                                "processorArchitecture": "x86_64"},
                 _dim(1.6667e-5, unit="Lambda-GB-Second")),
        _product("Serverless", {"group": "AWS-Lambda-Duration-ARM",
                                "processorArchitecture": "arm64"},
                 _dim(1.3334e-5, unit="Lambda-GB-Second")),
    ])
    assert fetcher.fetch_lambda("us-east-1", db=tmp_db) == 3
    assert set(_keys("AWSLambda", "us-east-1", tmp_db)) == {
        "lambda:requests", "lambda:duration:x86_64", "lambda:duration:arm64",
    }


def test_fetch_lambda_defaults_arch_to_x86(fake_pricing, tmp_db):
    fake_pricing([
        _product("Serverless", {"group": "AWS-Lambda-Duration"}, _dim(1.6667e-5)),
    ])
    fetcher.fetch_lambda("us-east-1", db=tmp_db)
    assert "lambda:duration:x86_64" in _keys("AWSLambda", "us-east-1", tmp_db)


def test_fetch_lambda_ignores_adjacent_duration_and_request_groups(fake_pricing, tmp_db):
    """
    Groups matched exactly, not by substring: ephemeral-storage duration and the
    Lambda@Edge rates would otherwise overwrite the real compute/request prices.
    """
    fake_pricing([
        _product("Serverless", {"group": "AWS-Lambda-Storage-Duration"}, _dim(3.09e-8)),
        _product("Serverless", {"group": "AWS-Lambda-Edge-Duration"}, _dim(5.0001e-6)),
        _product("Serverless", {"group": "AWS-Lambda-Edge-Requests"}, _dim(6e-7)),
        _product("Serverless", {"group": ""}, _dim(0.5)),
    ])
    assert fetcher.fetch_lambda("us-east-1", db=tmp_db) == 0
    assert _keys("AWSLambda", "us-east-1", tmp_db) == {}


# ── EC2 ──────────────────────────────────────────────────────────────────────


def test_fetch_ec2_key_format(fake_pricing, tmp_db):
    fake_pricing([
        _product("Compute Instance", {"instanceType": "t3.micro"}, _dim(0.0104)),
        _product("Compute Instance", {"instanceType": "m5.large"}, _dim(0.096)),
    ])
    assert fetcher.fetch_ec2_instances("us-east-1", db=tmp_db) == 2
    assert set(_keys("AmazonEC2", "us-east-1", tmp_db)) == {
        "ec2:t3.micro:linux:shared", "ec2:m5.large:linux:shared",
    }


def test_fetch_ec2_skips_missing_instance_type_and_other_families(fake_pricing, tmp_db):
    fake_pricing([
        _product("Compute Instance", {}, _dim(0.5)),
        _product("Dedicated Host", {"instanceType": "m5.large"}, _dim(2.0)),
    ])
    assert fetcher.fetch_ec2_instances("us-east-1", db=tmp_db) == 0


def test_fetch_ec2_filters_on_location_display_name(fake_pricing, tmp_db):
    """EC2 is an older service: it matches on `location`, not `regionCode`."""
    client = fake_pricing([])
    fetcher.fetch_ec2_instances("eu-west-2", db=tmp_db)
    fields = {f["Field"]: f["Value"] for f in client.filters}
    assert fields["location"] == "EU (London)"
    assert fields["operatingSystem"] == "Linux"
    assert fields["tenancy"] == "Shared"
    assert fields["capacitystatus"] == "Used"
    assert "regionCode" not in fields


# ── RDS ──────────────────────────────────────────────────────────────────────


def test_fetch_rds_key_includes_engine_and_deployment(fake_pricing, tmp_db):
    fake_pricing([
        _product("Database Instance", {"instanceType": "db.t3.medium",
                                       "databaseEngine": "PostgreSQL",
                                       "deploymentOption": "Single-AZ"}, _dim(0.068)),
        _product("Database Instance", {"instanceType": "db.t3.medium",
                                       "databaseEngine": "PostgreSQL",
                                       "deploymentOption": "Multi-AZ"}, _dim(0.136)),
    ])
    assert fetcher.fetch_rds_instances("us-east-1", db=tmp_db) == 2
    assert set(_keys("AmazonRDS", "us-east-1", tmp_db)) == {
        "rds:db.t3.medium:PostgreSQL:Single-AZ",
        "rds:db.t3.medium:PostgreSQL:Multi-AZ",
    }


def test_fetch_rds_defaults_deployment_to_single_az(fake_pricing, tmp_db):
    fake_pricing([
        _product("Database Instance", {"instanceType": "db.r5.large",
                                       "databaseEngine": "Aurora MySQL"}, _dim(0.29)),
    ])
    fetcher.fetch_rds_instances("us-east-1", db=tmp_db)
    assert "rds:db.r5.large:Aurora MySQL:Single-AZ" in _keys("AmazonRDS", "us-east-1", tmp_db)


def test_fetch_rds_engine_allowlist(fake_pricing, tmp_db):
    fake_pricing([
        _product("Database Instance", {"instanceType": "db.t3.medium",
                                       "databaseEngine": "Oracle"}, _dim(1.0)),
        _product("Database Instance", {"instanceType": "db.t3.medium",
                                       "databaseEngine": "SQL Server"}, _dim(1.0)),
        _product("Database Storage", {"instanceType": "db.t3.medium",
                                      "databaseEngine": "MySQL"}, _dim(0.115)),
    ])
    assert fetcher.fetch_rds_instances("us-east-1", db=tmp_db) == 0


# ── ElastiCache ──────────────────────────────────────────────────────────────


def test_fetch_elasticache_key_format(fake_pricing, tmp_db):
    fake_pricing([
        _product("Cache Instance", {"usagetype": "NodeUsage:cache.t3.micro",
                                     "instanceType": "cache.t3.micro",
                                     "cacheEngine": "Redis"}, _dim(0.017)),
        _product("Cache Instance", {"usagetype": "EUW2-NodeUsage:cache.m5.large",
                                     "instanceType": "cache.m5.large",
                                     "cacheEngine": "Memcached"}, _dim(0.156)),
    ])
    assert fetcher.fetch_elasticache("us-east-1", db=tmp_db) == 2
    assert set(_keys("AmazonElastiCache", "us-east-1", tmp_db)) == {
        "elasticache:cache.t3.micro:redis",
        "elasticache:cache.m5.large:memcached",
    }


def test_fetch_elasticache_excludes_surcharge_line_items(fake_pricing, tmp_db):
    """Extended Support / Sync Durability are additive charges on top of NodeUsage."""
    fake_pricing([
        _product("Cache Instance", {"usagetype": "ExtendedSupport:NodeUsage:cache.t3.micro",
                                     "instanceType": "cache.t3.micro",
                                     "cacheEngine": "Redis"}, _dim(0.005)),
        _product("Cache Instance", {"usagetype": "SyncDurability:NodeUsage:cache.t3.micro",
                                     "instanceType": "cache.t3.micro",
                                     "cacheEngine": "Redis"}, _dim(0.003)),
        _product("Cache Instance", {"usagetype": "SomethingElse:cache.t3.micro",
                                     "instanceType": "cache.t3.micro",
                                     "cacheEngine": "Redis"}, _dim(0.9)),
    ])
    assert fetcher.fetch_elasticache("us-east-1", db=tmp_db) == 0


def test_fetch_elasticache_requires_instance_type_and_engine(fake_pricing, tmp_db):
    fake_pricing([
        _product("Cache Instance", {"usagetype": "NodeUsage:cache.t3.micro",
                                     "cacheEngine": "Redis"}, _dim(0.017)),
        _product("Cache Instance", {"usagetype": "NodeUsage:cache.t3.micro",
                                     "instanceType": "cache.t3.micro"}, _dim(0.017)),
    ])
    assert fetcher.fetch_elasticache("us-east-1", db=tmp_db) == 0


# ── S3 ───────────────────────────────────────────────────────────────────────


def test_fetch_s3_maps_volume_type_to_slug(fake_pricing, tmp_db):
    fake_pricing([
        _product("Storage", {"volumeType": "Standard"}, _dim(0.023, unit="GB-Mo")),
        _product("Storage", {"volumeType": "Standard - Infrequent Access"},
                 _dim(0.0125, unit="GB-Mo")),
        _product("Storage", {"volumeType": "Amazon Glacier"}, _dim(0.0036, unit="GB-Mo")),
    ])
    assert fetcher.fetch_s3("us-east-1", db=tmp_db) == 3
    assert set(_keys("AmazonS3", "us-east-1", tmp_db)) == {
        "s3:storage:standard",
        "s3:storage:standard_ia",
        "s3:storage:glacier_flexible_retrieval",
    }


def test_fetch_s3_skips_unmapped_volume_types(fake_pricing, tmp_db):
    """Deep Archive and the granular Intelligent-Tiering tiers are deliberately out."""
    fake_pricing([
        _product("Storage", {"volumeType": "Glacier Deep Archive"}, _dim(0.00099)),
        _product("Storage", {"volumeType": "Intelligent-Tiering Archive Access"}, _dim(0.0036)),
        _product("Storage", {}, _dim(0.023)),
    ])
    assert fetcher.fetch_s3("us-east-1", db=tmp_db) == 0


def test_fetch_s3_uses_first_storage_tier(fake_pricing, tmp_db):
    """Standard storage is priced in declining GB tiers; keep the 0-50TB rate."""
    fake_pricing([
        _product("Storage", {"volumeType": "Standard"}, [
            _dim(0.021, unit="GB-Mo", begin_range="512000"),
            _dim(0.023, unit="GB-Mo", begin_range="0"),
        ]),
    ])
    fetcher.fetch_s3("us-east-1", db=tmp_db)
    assert _keys("AmazonS3", "us-east-1", tmp_db)["s3:storage:standard"] == pytest.approx(0.023)


# ── SQS ──────────────────────────────────────────────────────────────────────


def test_fetch_sqs_queue_types(fake_pricing, tmp_db):
    fake_pricing([
        _product("API Request", {"queueType": "Standard"}, _dim(4e-7, unit="Requests")),
        _product("API Request", {"queueType": "FIFO (first-in, first-out)"},
                 _dim(5e-7, unit="Requests")),
    ])
    assert fetcher.fetch_sqs("us-east-1", db=tmp_db) == 2
    assert set(_keys("AWSQueueService", "us-east-1", tmp_db)) == {
        "sqs:requests:standard", "sqs:requests:fifo",
    }


def test_fetch_sqs_skips_unknown_queue_type(fake_pricing, tmp_db):
    fake_pricing([_product("API Request", {"queueType": "Mystery"}, _dim(1e-6))])
    assert fetcher.fetch_sqs("us-east-1", db=tmp_db) == 0


# ── CloudWatch ───────────────────────────────────────────────────────────────


def test_fetch_cloudwatch_baseline_prices(fake_pricing, tmp_db):
    fake_pricing([
        _product("Alarm", {"usagetype": "USE1-CW:AlarmMonitorUsage"},
                 _dim(0.10, unit="Alarms")),
        _product("Metric", {"usagetype": "USE1-CW:MetricMonitorUsage"}, [
            _dim(0.10, begin_range="10000"),
            _dim(0.30, begin_range="0"),
        ]),
        _product("Data Payload", {"usagetype": "USE1-DataProcessing-Bytes",
                                  "group": "Ingested Logs"}, _dim(0.50, unit="GB")),
        _product("Storage Snapshot", {"usagetype": "USE1-TimedStorage-ByteHrs"},
                 _dim(0.03, unit="GB-Mo")),
    ])
    assert fetcher.fetch_cloudwatch("us-east-1", db=tmp_db) == 4
    prices = _keys("AmazonCloudWatch", "us-east-1", tmp_db)
    assert set(prices) == {
        "cloudwatch:alarm", "cloudwatch:metric",
        "cloudwatch:logs:ingestion", "cloudwatch:logs:storage",
    }
    # Custom metrics are tiered — the first-tier rate is the one that matters.
    assert prices["cloudwatch:metric"] == pytest.approx(0.30)


def test_fetch_cloudwatch_excludes_high_res_alarms(fake_pricing, tmp_db):
    """"CW:HighResAlarmMonitorUsage" must not be mistaken for the standard alarm rate."""
    fake_pricing([
        _product("Alarm", {"usagetype": "USE1-CW:HighResAlarmMonitorUsage"}, _dim(0.30)),
    ])
    assert fetcher.fetch_cloudwatch("us-east-1", db=tmp_db) == 0


def test_fetch_cloudwatch_storage_requires_snapshot_family(fake_pricing, tmp_db):
    """Other services' TimedStorage-ByteHrs line items share the suffix."""
    fake_pricing([
        _product("Data Payload", {"usagetype": "USE1-TimedStorage-ByteHrs"}, _dim(0.03)),
    ])
    assert fetcher.fetch_cloudwatch("us-east-1", db=tmp_db) == 0


def test_fetch_cloudwatch_ingestion_requires_ingested_logs_group(fake_pricing, tmp_db):
    fake_pricing([
        _product("Data Payload", {"usagetype": "USE1-DataProcessing-Bytes",
                                  "group": "Vended Logs"}, _dim(0.25)),
    ])
    assert fetcher.fetch_cloudwatch("us-east-1", db=tmp_db) == 0


def test_fetch_cloudwatch_ignores_niche_usage_types(fake_pricing, tmp_db):
    fake_pricing([
        _product("Metric", {"usagetype": "USE1-CW:Requests"}, _dim(1e-5)),
        _product("Synthetics", {"usagetype": "USE1-CW:Canary-runs"}, _dim(0.0012)),
    ])
    assert fetcher.fetch_cloudwatch("us-east-1", db=tmp_db) == 0


# ── EBS ──────────────────────────────────────────────────────────────────────


def test_fetch_ebs_storage_all_volume_types(fake_pricing, tmp_db):
    fake_pricing([
        _product("Storage", {"volumeApiName": "gp3"}, _dim(0.08, unit="GB-Mo")),
        _product("Storage", {"volumeApiName": "gp2"}, _dim(0.10, unit="GB-Mo")),
        _product("Storage", {"volumeApiName": "io1"}, _dim(0.125, unit="GB-Mo")),
        _product("Storage", {"volumeApiName": "io2"}, _dim(0.125, unit="GB-Mo")),
        _product("Storage", {"volumeApiName": "st1"}, _dim(0.045, unit="GB-Mo")),
        _product("Storage", {"volumeApiName": "sc1"}, _dim(0.015, unit="GB-Mo")),
        _product("Storage", {"volumeApiName": "standard"}, _dim(0.05, unit="GB-Mo")),
    ])
    assert fetcher.fetch_ebs("us-east-1", db=tmp_db) == 7
    assert set(_keys("AmazonEC2", "us-east-1", tmp_db)) == {
        "ebs:storage:gp3", "ebs:storage:gp2", "ebs:storage:io1", "ebs:storage:io2",
        "ebs:storage:st1", "ebs:storage:sc1", "ebs:storage:standard",
    }


def test_fetch_ebs_skips_unknown_volume_api_name(fake_pricing, tmp_db):
    fake_pricing([_product("Storage", {"volumeApiName": "outposts-thing"}, _dim(1.0))])
    assert fetcher.fetch_ebs("us-east-1", db=tmp_db) == 0


def test_fetch_ebs_iops_gp3_io1_and_io2_tiers(fake_pricing, tmp_db):
    fake_pricing([
        _product("System Operation", {"usagetype": "EBS:VolumeP-IOPS.gp3"}, _dim(0.005, unit="IOPS-Mo")),
        _product("System Operation", {"usagetype": "EBS:VolumeP-IOPS.piops"}, _dim(0.065, unit="IOPS-Mo")),
        _product("System Operation", {"usagetype": "EBS:VolumeP-IOPS.io2"}, _dim(0.065, unit="IOPS-Mo")),
        _product("System Operation", {"usagetype": "EBS:VolumeP-IOPS.io2.tier2"}, _dim(0.0455, unit="IOPS-Mo")),
        _product("System Operation", {"usagetype": "EBS:VolumeP-IOPS.io2.tier3"}, _dim(0.03185, unit="IOPS-Mo")),
    ])
    assert fetcher.fetch_ebs("us-east-1", db=tmp_db) == 5
    assert set(_keys("AmazonEC2", "us-east-1", tmp_db)) == {
        "ebs:iops:gp3", "ebs:iops:io1",
        "ebs:iops:io2:tier1", "ebs:iops:io2:tier2", "ebs:iops:io2:tier3",
    }


def test_fetch_ebs_ignores_unrelated_system_operation_usagetypes(fake_pricing, tmp_db):
    """The System Operation family also carries data transfer, snapshot copy, etc."""
    fake_pricing([
        _product("System Operation", {"usagetype": "EBS:VolumeIOUsage"}, _dim(5e-8)),
        _product("System Operation", {"usagetype": "USE1-EBS:TimeBasedSnapshotCopy.tier1"}, _dim(0.02)),
    ])
    assert fetcher.fetch_ebs("us-east-1", db=tmp_db) == 0


def test_fetch_ebs_throughput_normalized_from_gibps_to_mibps(fake_pricing, tmp_db):
    """API prices per GiBps-month; the pricer works in MiB/s (1 GiBps = 1024 MiBps)."""
    fake_pricing([
        _product("Provisioned Throughput", {"usagetype": "EBS:VolumeP-Throughput.gp3"},
                 _dim(40.96, unit="GiBps-mo")),
    ])
    assert fetcher.fetch_ebs("us-east-1", db=tmp_db) == 1
    row = price_db.get_price("AmazonEC2", "us-east-1", "ebs:throughput:gp3", db=tmp_db)
    assert row["unit"] == "MiBps-Mo"
    assert row["price_usd"] == pytest.approx(0.04)


def test_fetch_ebs_throughput_ignores_other_usagetypes(fake_pricing, tmp_db):
    fake_pricing([_product("Provisioned Throughput", {"usagetype": "SomethingElse"}, _dim(1.0))])
    assert fetcher.fetch_ebs("us-east-1", db=tmp_db) == 0


# ── ELB ──────────────────────────────────────────────────────────────────────


def test_fetch_elb_hourly_and_lcu_per_type(fake_pricing, tmp_db):
    fake_pricing([
        _product("Load Balancer-Application", {"usagetype": "USE1-LoadBalancerUsage"},
                 _dim(0.0225)),
        _product("Load Balancer-Application", {"usagetype": "USE1-LCUUsage"}, _dim(0.008)),
        _product("Load Balancer-Network", {"usagetype": "USE1-LoadBalancerUsage"},
                 _dim(0.0225)),
        _product("Load Balancer-Network", {"usagetype": "USE1-LCUUsage"}, _dim(0.006)),
    ])
    assert fetcher.fetch_elb("us-east-1", db=tmp_db) == 4
    assert set(_keys("AWSELB", "us-east-1", tmp_db)) == {
        "elb:hourly:application", "elb:lcu:application",
        "elb:hourly:network", "elb:lcu:network",
    }


def test_fetch_elb_excludes_outposts_and_trust_store(fake_pricing, tmp_db):
    """Both share the LoadBalancerUsage/LCUUsage suffix and would overwrite the real rate."""
    fake_pricing([
        _product("Load Balancer-Application",
                 {"usagetype": "USE1-Outposts-LoadBalancerUsage"}, _dim(0.9)),
        _product("Load Balancer-Application",
                 {"usagetype": "TS-USE1-LCUUsage"}, _dim(0.9)),
    ])
    assert fetcher.fetch_elb("us-east-1", db=tmp_db) == 0


def test_fetch_elb_excludes_reserved(fake_pricing, tmp_db):
    fake_pricing([
        _product("Load Balancer-Application",
                 {"usagetype": "USE1-Reserved LoadBalancerUsage"}, _dim(0.01)),
        _product("Load Balancer-Application",
                 {"usagetype": "USE1-ReservedLCUUsage"}, _dim(0.004)),
    ])
    assert fetcher.fetch_elb("us-east-1", db=tmp_db) == 0


def test_fetch_elb_classic_data_processing(fake_pricing, tmp_db):
    """Classic LBs bill data processed rather than LCUs."""
    fake_pricing([
        _product("Load Balancer", {"usagetype": "USE1-LoadBalancerUsage"}, _dim(0.025)),
        _product("Load Balancer", {"usagetype": "USE1-DataProcessing-Bytes"},
                 _dim(0.008, unit="GB")),
    ])
    assert fetcher.fetch_elb("us-east-1", db=tmp_db) == 2
    assert set(_keys("AWSELB", "us-east-1", tmp_db)) == {
        "elb:hourly:classic", "elb:data:classic",
    }


def test_fetch_elb_data_processing_only_for_classic(fake_pricing, tmp_db):
    fake_pricing([
        _product("Load Balancer-Application", {"usagetype": "USE1-DataProcessing-Bytes"},
                 _dim(0.008)),
    ])
    assert fetcher.fetch_elb("us-east-1", db=tmp_db) == 0


def test_fetch_elb_ignores_unknown_product_family(fake_pricing, tmp_db):
    fake_pricing([
        _product("Load Balancer-Fictional", {"usagetype": "USE1-LoadBalancerUsage"}, _dim(0.5)),
    ])
    assert fetcher.fetch_elb("us-east-1", db=tmp_db) == 0


# ── Secrets Manager ─────────────────────────────────────────────────────────


def test_fetch_secretsmanager_secret_and_requests(fake_pricing, tmp_db):
    fake_pricing([
        _product("Secret", {"usagetype": "USE1-AWSSecretsManager-Secrets"},
                 _dim(0.40, unit="Secrets")),
        _product("API Request", {"usagetype": "USE1-AWSSecretsManagerAPIRequest"},
                 _dim(0.000005, unit="API Requests")),
    ])
    assert fetcher.fetch_secretsmanager("us-east-1", db=tmp_db) == 2
    assert _keys("AWSSecretsManager", "us-east-1", tmp_db) == {
        "secretsmanager:secret": pytest.approx(0.40),
        "secretsmanager:requests": pytest.approx(0.000005),
    }


def test_fetch_secretsmanager_ignores_unknown_product_family(fake_pricing, tmp_db):
    fake_pricing([_product("Other", {"usagetype": "USE1-SomethingElse"}, _dim(1.0))])
    assert fetcher.fetch_secretsmanager("us-east-1", db=tmp_db) == 0


# ── Route 53 ─────────────────────────────────────────────────────────────────


def test_fetch_route53_hosted_zone_uses_first_tier(fake_pricing, tmp_db):
    fake_pricing([
        _product("DNS Zone", {"usagetype": "HostedZone"}, [
            _dim(0.10, unit="HostedZone", desc="additional zones", begin_range="25"),
            _dim(0.50, unit="HostedZone", desc="first 25 zones", begin_range="0"),
        ]),
    ])
    assert fetcher.fetch_route53("us-east-1", db=tmp_db) == 1
    row = price_db.get_price("AmazonRoute53", "us-east-1", "route53:hostedzone", db=tmp_db)
    assert row["price_usd"] == pytest.approx(0.50)


def test_fetch_route53_ignores_extra_rrsets(fake_pricing, tmp_db):
    fake_pricing([_product("DNS Zone", {"usagetype": "Global-RRSets"}, _dim(0.0015))])
    assert fetcher.fetch_route53("us-east-1", db=tmp_db) == 0


def test_fetch_route53_standard_queries_first_tier(fake_pricing, tmp_db):
    fake_pricing([
        _product("DNS Query", {
            "usagetype": "DNS-Queries", "routingType": "Standard", "routingTarget": "External",
        }, [
            _dim(0.0000002, unit="Queries", desc="over 1B", begin_range="1000000000"),
            _dim(0.0000004, unit="Queries", desc="first 1B", begin_range="0"),
        ]),
    ])
    assert fetcher.fetch_route53("us-east-1", db=tmp_db) == 1
    row = price_db.get_price("AmazonRoute53", "us-east-1", "route53:queries", db=tmp_db)
    assert row["price_usd"] == pytest.approx(0.0000004)


def test_fetch_route53_ignores_resolver_query_pricing(fake_pricing, tmp_db):
    """Resolver's region-scoped query pricing shares the DNS Query family but has
    no routingType attribute and a region-prefixed usagetype."""
    fake_pricing([_product("DNS Query", {"usagetype": "USE1-DNS-Queries"}, _dim(0.0000004))])
    assert fetcher.fetch_route53("us-east-1", db=tmp_db) == 0


def test_fetch_route53_ignores_non_standard_routing(fake_pricing, tmp_db):
    fake_pricing([
        _product("DNS Query", {
            "usagetype": "LBR-Queries", "routingType": "Latency Based Routing", "routingTarget": "External",
        }, _dim(0.0000006)),
    ])
    assert fetcher.fetch_route53("us-east-1", db=tmp_db) == 0


# ── KMS ──────────────────────────────────────────────────────────────────────


def test_fetch_kms_key_and_standard_requests(fake_pricing, tmp_db):
    fake_pricing([
        _product("Encryption Key", {"usagetype": "USE1-KMS-Keys"}, _dim(1.0, unit="Keys")),
        _product("API Request", {"usagetype": "USE1-KMS-Requests"}, _dim(0.000003, unit="Requests")),
    ])
    assert fetcher.fetch_kms("us-east-1", db=tmp_db) == 2
    assert _keys("awskms", "us-east-1", tmp_db) == {
        "kms:key": pytest.approx(1.0),
        "kms:requests": pytest.approx(0.000003),
    }


def test_fetch_kms_ignores_asymmetric_and_datakeypair_requests(fake_pricing, tmp_db):
    """These carry no productFamily at all and cost 5-400x the standard rate —
    a substring match on usagetype would risk picking one up instead."""
    fake_pricing([
        _product("", {"usagetype": "USE1-KMS-Requests-Asymmetric-RSA_2048"}, _dim(0.000003)),
        _product("", {"usagetype": "USE1-KMS-Requests-Asymmetric"}, _dim(0.000015)),
        _product("", {"usagetype": "USE1-KMS-Requests-GenerateDatakeyPair-RSA"}, _dim(0.0012)),
        _product("", {"usagetype": "USE1-KMS-Requests-GenerateDatakeyPair-ECC"}, _dim(0.00001)),
    ])
    assert fetcher.fetch_kms("us-east-1", db=tmp_db) == 0


# ── WAF ──────────────────────────────────────────────────────────────────────


def test_fetch_waf_webacl_rule_and_baseline_request(fake_pricing, tmp_db):
    fake_pricing([
        _product("Web Application Firewall",
                 {"usagetype": "USE1-WebACLV2", "group": "Web ACL"}, _dim(5.0, unit="Month")),
        _product("Web Application Firewall",
                 {"usagetype": "USE1-RuleV2", "group": "Rule"}, _dim(1.0, unit="Month")),
        _product("Web Application Firewall",
                 {"usagetype": "USE1-RequestV2-Tier0", "group": "Request"},
                 _dim(0.0000006, unit="Request")),
    ])
    assert fetcher.fetch_waf("us-east-1", db=tmp_db) == 3
    assert _keys("awswaf", "us-east-1", tmp_db) == {
        "waf:webacl": pytest.approx(5.0),
        "waf:rule": pytest.approx(1.0),
        "waf:requests": pytest.approx(0.0000006),
    }


def test_fetch_waf_ignores_classic_webacl_and_rule(fake_pricing, tmp_db):
    """Classic WAF (aws_waf, not aws_wafv2) shares the same $5/$1 prices under
    non-V2 usagetypes — bucksawz only supports aws_wafv2_web_acl."""
    fake_pricing([
        _product("Web Application Firewall",
                 {"usagetype": "USE1-WebACL", "group": "Web ACL"}, _dim(5.0)),
        _product("Web Application Firewall",
                 {"usagetype": "USE1-Rule", "group": "Rule"}, _dim(1.0)),
    ])
    assert fetcher.fetch_waf("us-east-1", db=tmp_db) == 0


def test_fetch_waf_ignores_shield_protected_and_higher_wcu_tiers(fake_pricing, tmp_db):
    fake_pricing([
        _product("Web Application Firewall",
                 {"usagetype": "USE1-ShieldProtected-RequestV2-Tier2-2000WCU",
                  "group": "Request (Shield Protected)"}, _dim(0.0000002)),
        _product("Web Application Firewall",
                 {"usagetype": "USE1-RequestV2-Tier4-3000WCU", "group": "Request"}, _dim(0.0000012)),
        _product("Web Application Firewall",
                 {"usagetype": "USE1-AMR-BotControl-Request", "group": "AMR Bot Control Request"},
                 _dim(0.000001)),
    ])
    assert fetcher.fetch_waf("us-east-1", db=tmp_db) == 0


# ── fetch_all ────────────────────────────────────────────────────────────────


def test_all_services_matches_fetcher_registry():
    assert set(fetcher.ALL_SERVICES) == {
        "ECS", "Lambda", "EC2", "EBS", "RDS", "ElastiCache", "S3", "SQS", "CloudWatch", "ELB",
        "SecretsManager", "Route53", "KMS", "WAF", "DataTransfer",
    }


def test_prices_update_help_lists_every_service():
    """`--services` help text spells the names out, so it has to stay in sync."""
    from bucksawz.cli import prices_update
    help_text = next(p for p in prices_update.params if p.name == "services").help
    for svc in fetcher.ALL_SERVICES:
        assert svc in help_text


def test_fetch_all_sums_across_regions(monkeypatch, tmp_db):
    calls = []

    def _fake_fetch(region, profile=None, db=None):
        calls.append(region)
        return 3

    monkeypatch.setitem(fetcher._FETCHERS, "EC2", _fake_fetch)
    totals = fetcher.fetch_all(["us-east-1", "eu-west-2"], ["EC2"], db=tmp_db)
    assert totals == {"EC2": 6}
    assert calls == ["us-east-1", "eu-west-2"]


def test_fetch_all_skips_unknown_service(tmp_db, capsys):
    totals = fetcher.fetch_all(["us-east-1"], ["NotAService"], db=tmp_db)
    assert totals == {}
    assert "unknown service" in capsys.readouterr().out


def test_fetch_all_survives_one_region_failing(monkeypatch, tmp_db, capsys):
    def _flaky(region, profile=None, db=None):
        if region == "eu-west-2":
            raise RuntimeError("throttled")
        return 5

    monkeypatch.setitem(fetcher._FETCHERS, "EC2", _flaky)
    totals = fetcher.fetch_all(["us-east-1", "eu-west-2"], ["EC2"], db=tmp_db)
    assert totals == {"EC2": 5}
    assert "throttled" in capsys.readouterr().out


def test_fetch_all_defaults_to_every_service(monkeypatch, tmp_db):
    seen = []
    for svc in list(fetcher._FETCHERS):
        monkeypatch.setitem(
            fetcher._FETCHERS, svc,
            lambda region, profile=None, db=None, _s=svc: (seen.append(_s), 1)[1],
        )
    totals = fetcher.fetch_all(["us-east-1"], db=tmp_db)
    assert seen == fetcher.ALL_SERVICES
    assert set(totals) == set(fetcher.ALL_SERVICES)


# ── Data Transfer ────────────────────────────────────────────────────────────


def test_fetch_data_transfer_stores_all_outbound_tiers(fake_pricing, tmp_db):
    fake_pricing([
        _product("Data Transfer", {"transferType": "AWS Outbound"}, [
            _dim(0.09, unit="GB", desc="first 10TB", begin_range="0"),
            _dim(0.085, unit="GB", desc="next 40TB", begin_range="10240"),
            _dim(0.07, unit="GB", desc="next 100TB", begin_range="51200"),
            _dim(0.05, unit="GB", desc="over 150TB", begin_range="153600"),
        ]),
    ])
    assert fetcher.fetch_data_transfer("us-east-1", db=tmp_db) == 4
    assert _keys("AmazonEC2", "us-east-1", tmp_db) == {
        "datatransfer:out:0": pytest.approx(0.09),
        "datatransfer:out:10240": pytest.approx(0.085),
        "datatransfer:out:51200": pytest.approx(0.07),
        "datatransfer:out:153600": pytest.approx(0.05),
    }


def test_fetch_data_transfer_stores_flat_inter_az_rate(fake_pricing, tmp_db):
    fake_pricing([
        _product("Data Transfer", {"transferType": "IntraRegion"}, _dim(0.01, unit="GB")),
    ])
    assert fetcher.fetch_data_transfer("us-east-1", db=tmp_db) == 1
    assert _keys("AmazonEC2", "us-east-1", tmp_db) == {
        "datatransfer:regional": pytest.approx(0.01),
    }


def test_fetch_data_transfer_ignores_free_inbound(fake_pricing, tmp_db):
    fake_pricing([
        _product("Data Transfer", {"transferType": "AWS Inbound"}, _dim(0.0, unit="GB")),
    ])
    assert fetcher.fetch_data_transfer("us-east-1", db=tmp_db) == 0


def test_fetch_data_transfer_ignores_interregion(fake_pricing, tmp_db):
    """Region-to-region transfer shares the same product family but depends on
    a (from, to) region pair Terraform config can't express."""
    fake_pricing([
        _product("Data Transfer", {"transferType": "InterRegion Outbound"}, _dim(0.02, unit="GB")),
    ])
    assert fetcher.fetch_data_transfer("us-east-1", db=tmp_db) == 0
