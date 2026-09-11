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


# ── NAT Gateway ──────────────────────────────────────────────────────────────


def test_fetch_nat_gateway_hourly_and_data(fake_pricing, tmp_db):
    fake_pricing([
        _product("NAT Gateway", {"usagetype": "USE1-NatGateway-Hours"}, _dim(0.045)),
        _product("NAT Gateway", {"usagetype": "USE1-NatGateway-Bytes"},
                 _dim(0.045, unit="GB")),
    ])
    assert fetcher.fetch_nat_gateway("us-east-1", db=tmp_db) == 2
    assert set(_keys("AmazonEC2", "us-east-1", tmp_db)) == {
        "natgateway:hourly", "natgateway:data",
    }


def test_fetch_nat_gateway_excludes_outposts(fake_pricing, tmp_db):
    """Outposts NAT Gateway shares the same usagetype suffix and family."""
    fake_pricing([
        _product("NAT Gateway", {"usagetype": "USE1-Outposts-NatGateway-Hours"}, _dim(0.9)),
        _product("NAT Gateway", {"usagetype": "USE1-Outposts-NatGateway-Bytes"}, _dim(0.9)),
    ])
    assert fetcher.fetch_nat_gateway("us-east-1", db=tmp_db) == 0


def test_fetch_nat_gateway_ignores_unknown_usagetype(fake_pricing, tmp_db):
    fake_pricing([
        _product("NAT Gateway", {"usagetype": "USE1-SomethingElse"}, _dim(0.5)),
    ])
    assert fetcher.fetch_nat_gateway("us-east-1", db=tmp_db) == 0


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


# ── EKS ──────────────────────────────────────────────────────────────────────


def test_fetch_eks_cluster_hourly(fake_pricing, tmp_db):
    fake_pricing([
        _product("Compute", {"usagetype": "AmazonEKS-Hours:perCluster"}, _dim(0.10)),
    ])
    assert fetcher.fetch_eks("us-east-1", db=tmp_db) == 1
    assert _keys("AmazonEKS", "us-east-1", tmp_db) == {"eks:cluster": pytest.approx(0.10)}


def test_fetch_eks_ignores_extended_support(fake_pricing, tmp_db):
    """Extended-support clusters (older k8s versions) bill several times
    more per hour under the same productFamily and a usagetype that only
    differs by an "Extended" suffix — an endswith(":perCluster") substring
    test would wrongly accept it too if it didn't anchor on the exact tail."""
    fake_pricing([
        _product("Compute", {"usagetype": "AmazonEKS-Hours:perClusterExtended"}, _dim(0.60)),
    ])
    assert fetcher.fetch_eks("us-east-1", db=tmp_db) == 0


# ── DynamoDB ─────────────────────────────────────────────────────────────────


def test_fetch_dynamodb_storage_provisioned_and_ondemand(fake_pricing, tmp_db):
    fake_pricing([
        _product("Database Storage", {"usagetype": "TimedStorage-ByteHrs"}, _dim(0.25, unit="GB-Mo")),
        _product("Provisioned Throughput", {"usagetype": "ReadCapacityUnit-Hrs"}, _dim(0.00013)),
        _product("Provisioned Throughput", {"usagetype": "WriteCapacityUnit-Hrs"}, _dim(0.00065)),
        _product("API Request", {"usagetype": "ReadRequestUnits"}, _dim(0.000000125, unit="Requests")),
        _product("API Request", {"usagetype": "WriteRequestUnits"}, _dim(0.000000625, unit="Requests")),
    ])
    assert fetcher.fetch_dynamodb("us-east-1", db=tmp_db) == 5
    assert _keys("AmazonDynamoDB", "us-east-1", tmp_db) == {
        "dynamodb:storage": pytest.approx(0.25),
        "dynamodb:provisioned:read": pytest.approx(0.00013),
        "dynamodb:provisioned:write": pytest.approx(0.00065),
        "dynamodb:ondemand:read": pytest.approx(0.000000125),
        "dynamodb:ondemand:write": pytest.approx(0.000000625),
    }


def test_fetch_dynamodb_ignores_replicated_and_pitr(fake_pricing, tmp_db):
    """Global Tables' replicated capacity/request units, and PITR backup
    storage, share a usagetype suffix with the base metrics under a plain
    endswith test."""
    fake_pricing([
        _product("Provisioned Throughput", {"usagetype": "ReplicatedReadCapacityUnit-Hrs"}, _dim(0.0002)),
        _product("Provisioned Throughput", {"usagetype": "ReplicatedWriteCapacityUnit-Hrs"}, _dim(0.0013)),
        _product("API Request", {"usagetype": "ReplicatedWriteRequestUnits"}, _dim(0.0000019)),
        _product("Database Storage", {"usagetype": "TimedPITRStorage-ByteHrs"}, _dim(0.20, unit="GB-Mo")),
    ])
    assert fetcher.fetch_dynamodb("us-east-1", db=tmp_db) == 0


# ── VPC Interface Endpoint ───────────────────────────────────────────────────


def test_fetch_vpc_endpoint_hourly_and_data(fake_pricing, tmp_db):
    fake_pricing([
        _product("VpcEndpoint", {"usagetype": "VpcEndpoint-Hours"}, _dim(0.01)),
        _product("VpcEndpoint", {"usagetype": "VpcEndpoint-Bytes"}, _dim(0.01, unit="GB")),
    ])
    assert fetcher.fetch_vpc_endpoint("us-east-1", db=tmp_db) == 2
    assert _keys("AmazonVPC", "us-east-1", tmp_db) == {
        "vpcendpoint:hourly": pytest.approx(0.01),
        "vpcendpoint:data": pytest.approx(0.01),
    }


# ── SNS ──────────────────────────────────────────────────────────────────────


def test_fetch_sns_standard_requests(fake_pricing, tmp_db):
    fake_pricing([
        _product("API Request", {"usagetype": "Requests-Tier1"}, _dim(0.0000005, unit="Requests")),
    ])
    assert fetcher.fetch_sns("us-east-1", db=tmp_db) == 1
    assert _keys("AmazonSNS", "us-east-1", tmp_db) == {"sns:requests": pytest.approx(0.0000005)}


def test_fetch_sns_ignores_non_tier1_requests(fake_pricing, tmp_db):
    fake_pricing([
        _product("API Request", {"usagetype": "SMS-Requests"}, _dim(0.00645)),
        _product("API Request", {"usagetype": "Email-Requests"}, _dim(0.000002)),
    ])
    assert fetcher.fetch_sns("us-east-1", db=tmp_db) == 0


# ── EFS ──────────────────────────────────────────────────────────────────────


def test_fetch_efs_standard_storage(fake_pricing, tmp_db):
    fake_pricing([
        _product("Storage", {"usagetype": "TimedStorage-ByteHrs"}, _dim(0.30, unit="GB-Mo")),
    ])
    assert fetcher.fetch_efs("us-east-1", db=tmp_db) == 1
    assert _keys("AmazonEFS", "us-east-1", tmp_db) == {"efs:storage:standard": pytest.approx(0.30)}


def test_fetch_efs_ignores_ia_and_onezone(fake_pricing, tmp_db):
    fake_pricing([
        _product("Storage", {"usagetype": "TimedStorage-IA-ByteHrs"}, _dim(0.025, unit="GB-Mo")),
        _product("Storage", {"usagetype": "TimedStorage-OneZone-ByteHrs"}, _dim(0.16, unit="GB-Mo")),
        _product("Storage", {"usagetype": "TimedStorage-OneZone-IA-ByteHrs"}, _dim(0.0133, unit="GB-Mo")),
    ])
    assert fetcher.fetch_efs("us-east-1", db=tmp_db) == 0


# ── ECR ──────────────────────────────────────────────────────────────────────


def test_fetch_ecr_storage(fake_pricing, tmp_db):
    fake_pricing([
        _product("Storage", {"usagetype": "TimedStorage-ByteHrs"}, _dim(0.10, unit="GB-Mo")),
    ])
    assert fetcher.fetch_ecr("us-east-1", db=tmp_db) == 1
    assert _keys("AmazonECR", "us-east-1", tmp_db) == {"ecr:storage": pytest.approx(0.10)}


# ── API Gateway ──────────────────────────────────────────────────────────────


def test_fetch_apigateway_rest_and_http_first_tier(fake_pricing, tmp_db):
    fake_pricing([
        _product("API Calls", {"usagetype": "ApiGatewayRequest"},
                 [_dim(0.0000035, unit="Requests", begin_range="0"),
                  _dim(0.0000028, unit="Requests", begin_range="333000000")]),
        _product("API Calls", {"usagetype": "ApiGatewayHttpApi"},
                 [_dim(0.0000010, unit="Requests", begin_range="0"),
                  _dim(0.0000009, unit="Requests", begin_range="300000000")]),
    ])
    assert fetcher.fetch_apigateway("us-east-1", db=tmp_db) == 2
    assert _keys("AmazonApiGateway", "us-east-1", tmp_db) == {
        "apigateway:rest:requests": pytest.approx(0.0000035),
        "apigateway:http:requests": pytest.approx(0.0000010),
    }


def test_fetch_apigateway_ignores_websocket(fake_pricing, tmp_db):
    fake_pricing([
        _product("API Calls", {"usagetype": "ApiGatewayMessage"}, _dim(0.000001)),
        _product("API Calls", {"usagetype": "ApiGatewayMinute"}, _dim(0.00025)),
    ])
    assert fetcher.fetch_apigateway("us-east-1", db=tmp_db) == 0


# ── CloudFront ───────────────────────────────────────────────────────────────


def test_fetch_cloudfront_us_data_transfer_and_https_requests(fake_pricing, tmp_db):
    fake_pricing([
        _product("Data Transfer",
                 {"location": "United States", "usagetype": "DataTransfer-Out-Bytes"},
                 [_dim(0.085, unit="GB", begin_range="0"),
                  _dim(0.080, unit="GB", begin_range="10240")]),
        _product("Request",
                 {"location": "United States", "usagetype": "Requests-HTTPS-Proxy"},
                 [_dim(0.0000100, unit="Requests", begin_range="0")]),
    ])
    assert fetcher.fetch_cloudfront("us-east-1", db=tmp_db) == 2
    assert _keys("AmazonCloudFront", "us-east-1", tmp_db) == {
        "cloudfront:data:out": pytest.approx(0.085),
        "cloudfront:requests:https": pytest.approx(0.0000100),
    }


def test_fetch_cloudfront_ignores_non_us_location_group(fake_pricing, tmp_db):
    fake_pricing([
        _product("Data Transfer",
                 {"location": "India", "usagetype": "IN-DataTransfer-Out-Bytes"}, _dim(0.170, unit="GB")),
        _product("Request",
                 {"location": "South America", "usagetype": "SA-Requests-HTTPS-Proxy"}, _dim(0.0000160)),
    ])
    assert fetcher.fetch_cloudfront("us-east-1", db=tmp_db) == 0


# ── Kinesis ──────────────────────────────────────────────────────────────────


def test_fetch_kinesis_shard_hour_and_payload_units(fake_pricing, tmp_db):
    fake_pricing([
        _product("Kinesis Streams", {"usagetype": "ShardHour"}, _dim(0.015)),
        _product("Kinesis Streams", {"usagetype": "PayloadUnits"}, _dim(0.000000014, unit="Units")),
    ])
    assert fetcher.fetch_kinesis("us-east-1", db=tmp_db) == 2
    assert _keys("AmazonKinesis", "us-east-1", tmp_db) == {
        "kinesis:shard:hour": pytest.approx(0.015),
        "kinesis:payload:units": pytest.approx(0.000000014),
    }


def test_fetch_kinesis_ignores_extended_retention(fake_pricing, tmp_db):
    fake_pricing([
        _product("Kinesis Streams", {"usagetype": "ExtendedDataRetention"}, _dim(0.02)),
    ])
    assert fetcher.fetch_kinesis("us-east-1", db=tmp_db) == 0


# ── Step Functions ───────────────────────────────────────────────────────────


def test_fetch_stepfunctions_standard_and_express(fake_pricing, tmp_db):
    fake_pricing([
        _product("", {"usagetype": "StateTransition"}, _dim(0.000025)),
        _product("", {"usagetype": "StepFunctions-Request"}, _dim(0.000001)),
        _product("", {"usagetype": "StepFunctions-GB-Second"}, _dim(0.00001042, unit="GB-Second")),
    ])
    assert fetcher.fetch_stepfunctions("us-east-1", db=tmp_db) == 3
    assert _keys("AmazonStates", "us-east-1", tmp_db) == {
        "sfn:standard:transitions": pytest.approx(0.000025),
        "sfn:express:requests": pytest.approx(0.000001),
        "sfn:express:duration": pytest.approx(0.00001042),
    }


# ── EventBridge ──────────────────────────────────────────────────────────────


def test_fetch_eventbridge_custom_events(fake_pricing, tmp_db):
    fake_pricing([
        _product("", {"usagetype": "PutEvents-Event-64K-Chunks"}, _dim(1.00, unit="Events")),
    ])
    assert fetcher.fetch_eventbridge("us-east-1", db=tmp_db) == 1
    assert _keys("AWSEvents", "us-east-1", tmp_db) == {"eventbridge:events": pytest.approx(1.00)}


# ── Transit Gateway ──────────────────────────────────────────────────────────


def test_fetch_transit_gateway_hourly_and_data(fake_pricing, tmp_db):
    fake_pricing([
        _product("Transit Gateway", {"usagetype": "TransitGateway-Hours"}, _dim(0.05)),
        _product("Transit Gateway", {"usagetype": "TransitGateway-Bytes"}, _dim(0.02, unit="GB")),
    ])
    assert fetcher.fetch_transit_gateway("us-east-1", db=tmp_db) == 2
    assert _keys("AmazonVPC", "us-east-1", tmp_db) == {
        "transitgateway:hourly": pytest.approx(0.05),
        "transitgateway:data": pytest.approx(0.02),
    }


# ── S3 Files ─────────────────────────────────────────────────────────────────


def test_fetch_s3files_storage_write_and_read(fake_pricing, tmp_db):
    fake_pricing([
        _product("Storage", {"usagetype": "USE1-Files-TimedStorage-ByteHrs"}, _dim(0.30, unit="GB-Mo")),
        _product(None, {"usagetype": "USE1-Files-Write"}, _dim(0.06, unit="GB")),
        _product(None, {"usagetype": "USE1-Files-Read"}, _dim(0.03, unit="GB")),
    ])
    assert fetcher.fetch_s3files("us-east-1", db=tmp_db) == 3
    assert _keys("AmazonS3", "us-east-1", tmp_db) == {
        "s3files:storage": pytest.approx(0.30),
        "s3files:write": pytest.approx(0.06),
        "s3files:read": pytest.approx(0.03),
    }


def test_fetch_s3files_ignores_unrelated_s3_line_items(fake_pricing, tmp_db):
    """Plain S3 storage/request line items share the AmazonS3 service code
    but must not collide with the Files-suffixed usagetypes."""
    fake_pricing([
        _product("Storage", {"usagetype": "TimedStorage-ByteHrs"}, _dim(0.023, unit="GB-Mo")),
        _product("API Request", {"usagetype": "Requests-Tier1"}, _dim(0.000005, unit="Requests")),
    ])
    assert fetcher.fetch_s3files("us-east-1", db=tmp_db) == 0
    assert _keys("AmazonS3", "us-east-1", tmp_db) == {}


# ── OpenSearch ───────────────────────────────────────────────────────────────


def test_fetch_opensearch_instance_and_storage(fake_pricing, tmp_db):
    fake_pricing([
        _product("Amazon OpenSearch Service Instance", {"instanceType": "r6g.large.search"}, _dim(0.167)),
        _product(
            "Amazon OpenSearch Service Volume",
            {"usagetype": "ES:GP3-Storage"},
            _dim(0.112, unit="GB-Mo"),
        ),
        _product(
            "Amazon OpenSearch Service Volume",
            {"usagetype": "ES:GP2-Storage"},
            _dim(0.135, unit="GB-Mo"),
        ),
    ])
    assert fetcher.fetch_opensearch("us-east-1", db=tmp_db) == 3
    assert _keys("AmazonES", "us-east-1", tmp_db) == {
        "opensearch:r6g.large.search": pytest.approx(0.167),
        "opensearch:storage:gp3": pytest.approx(0.112),
        "opensearch:storage:gp2": pytest.approx(0.135),
    }


def test_fetch_opensearch_io1_and_standard_storage(fake_pricing, tmp_db):
    fake_pricing([
        _product("Amazon OpenSearch Service Volume", {"usagetype": "ES:PIOPS-Storage"}, _dim(0.15, unit="GB-Mo")),
        _product("Amazon OpenSearch Service Volume", {"usagetype": "ES:Magnetic-Storage"}, _dim(0.05, unit="GB-Mo")),
    ])
    assert fetcher.fetch_opensearch("us-east-1", db=tmp_db) == 2
    assert _keys("AmazonES", "us-east-1", tmp_db) == {
        "opensearch:storage:io1": pytest.approx(0.15),
        "opensearch:storage:standard": pytest.approx(0.05),
    }


def test_fetch_opensearch_rejects_unrelated_volume_usagetypes(fake_pricing, tmp_db):
    """GP3 IOPS/throughput add-ons, the bare PIOPS-hour rate, UltraWarm's
    managed storage, and the vector-search add-on all share the "...Volume"
    family but aren't `ebs_options` per-GB storage."""
    fake_pricing([
        _product("Amazon OpenSearch Service Instance", {"instanceType": "r6g.large.search"}, _dim(0.167)),
        _product("Amazon OpenSearch Service Volume", {"usagetype": "ES:GP3-PIOPS"}, _dim(0.008)),
        _product("Amazon OpenSearch Service Volume", {"usagetype": "ES:GP3-Provisioned-ThroughPut"}, _dim(0.04)),
        _product("Amazon OpenSearch Service Volume", {"usagetype": "ES:PIOPS"}, _dim(0.10)),
        _product("Amazon OpenSearch Service Volume", {"usagetype": "ES:Managed-Storage"}, _dim(0.024)),
        _product("Amazon OpenSearch Service Volume", {"usagetype": "OpenSearch-Vectors-TimedStorage-ByteHrs"}, _dim(0.02)),
    ])
    assert fetcher.fetch_opensearch("us-east-1", db=tmp_db) == 1
    assert _keys("AmazonES", "us-east-1", tmp_db) == {
        "opensearch:r6g.large.search": pytest.approx(0.167),
    }


# ── Redshift ─────────────────────────────────────────────────────────────────


def test_fetch_redshift_compute_and_storage(fake_pricing, tmp_db):
    fake_pricing([
        _product("Compute Instance", {"instanceType": "ra3.xlplus"}, _dim(1.086)),
        _product("Storage", {"usagetype": "RMS:StorageUsage"}, _dim(0.024, unit="GB-Mo")),
    ])
    assert fetcher.fetch_redshift("us-east-1", db=tmp_db) == 2
    assert _keys("AmazonRedshift", "us-east-1", tmp_db) == {
        "redshift:ra3.xlplus": pytest.approx(1.086),
        "redshift:storage": pytest.approx(0.024),
    }


def test_fetch_redshift_rejects_unrelated_storage_usagetypes(fake_pricing, tmp_db):
    """Backup/snapshot storage shares the "Storage" family with managed
    storage but is a distinct, unrelated charge and must not collide."""
    fake_pricing([
        _product("Compute Instance", {"instanceType": "dc2.large"}, _dim(0.25)),
        _product("Storage", {"usagetype": "BackupUsage"}, _dim(0.023, unit="GB-Mo")),
    ])
    assert fetcher.fetch_redshift("us-east-1", db=tmp_db) == 1
    assert _keys("AmazonRedshift", "us-east-1", tmp_db) == {
        "redshift:dc2.large": pytest.approx(0.25),
    }


# ── AWS Backup ───────────────────────────────────────────────────────────────


def test_fetch_backup_storage_and_restore(fake_pricing, tmp_db):
    fake_pricing([
        _product("", {"usagetype": "Warm-Storage-ByteHrs"}, _dim(0.05, unit="GB-Mo")),
        _product("", {"usagetype": "Cold-Storage-ByteHrs"}, _dim(0.01, unit="GB-Mo")),
        _product("", {"usagetype": "Restore-Bytes"}, _dim(0.02, unit="GB")),
    ])
    assert fetcher.fetch_backup("us-east-1", db=tmp_db) == 3
    assert _keys("AWSBackup", "us-east-1", tmp_db) == {
        "backup:storage:warm": pytest.approx(0.05),
        "backup:storage:cold": pytest.approx(0.01),
        "backup:restore": pytest.approx(0.02),
    }


# ── MSK ──────────────────────────────────────────────────────────────────────


def test_fetch_msk_broker_and_storage(fake_pricing, tmp_db):
    fake_pricing([
        _product(
            "Managed Streaming for Apache Kafka (MSK)",
            {"usagetype": "USE1-Kafka.m5.large"},
            _dim(0.21),
        ),
        _product(
            "Managed Streaming for Apache Kafka (MSK)",
            {"usagetype": "USE1-Kafka.Storage.GP2"},
            _dim(0.10, unit="GB-Mo"),
        ),
    ])
    assert fetcher.fetch_msk("us-east-1", db=tmp_db) == 2
    assert _keys("AmazonMSK", "us-east-1", tmp_db) == {
        "msk:kafka.m5.large": pytest.approx(0.21),
        "msk:storage": pytest.approx(0.10),
    }


def test_fetch_msk_ignores_serverless_capacity_and_tiered_retrieval(fake_pricing, tmp_db):
    """Serverless capacity units, tiered-storage retrieval, private
    connectivity, and throughput all share MSK's single product family but
    aren't standard broker instance-hours or plain EBS storage."""
    fake_pricing([
        _product(
            "Managed Streaming for Apache Kafka (MSK)",
            {"usagetype": "USE1-Kafka.mcu.general"},
            _dim(0.0015),
        ),
        _product(
            "Managed Streaming for Apache Kafka (MSK)",
            {"usagetype": "USE1-Kafka.Storage.Tiered"},
            _dim(0.01, unit="GB-Mo"),
        ),
        _product(
            "Managed Streaming for Apache Kafka (MSK)",
            {"usagetype": "USE1-Kafka.Throughput"},
            _dim(0.0015),
        ),
        _product(
            "Managed Streaming for Apache Kafka (MSK)",
            {"usagetype": "USE1-Kafka.PrivateConnectivityHours"},
            _dim(0.01),
        ),
    ])
    assert fetcher.fetch_msk("us-east-1", db=tmp_db) == 0
    assert _keys("AmazonMSK", "us-east-1", tmp_db) == {}


# ── Elastic IP ───────────────────────────────────────────────────────────────


def test_fetch_eip_flat_rate(fake_pricing, tmp_db):
    fake_pricing([
        _product("IP Address", {"usagetype": "PublicIPv4:InUseAddress"}, _dim(0.005)),
    ])
    assert fetcher.fetch_eip("us-east-1", db=tmp_db) == 1
    assert _keys("AmazonVPC", "us-east-1", tmp_db) == {"eip:hourly": pytest.approx(0.005)}


def test_fetch_eip_ignores_unrelated_ip_address_line_items(fake_pricing, tmp_db):
    fake_pricing([
        _product("IP Address", {"usagetype": "SomeOtherIpCharge"}, _dim(1.23)),
    ])
    assert fetcher.fetch_eip("us-east-1", db=tmp_db) == 0
    assert _keys("AmazonVPC", "us-east-1", tmp_db) == {}


def test_fetch_eip_ignores_contiguous_block_and_idle_variants(fake_pricing, tmp_db):
    """BYOIP pool pricing and the (now identically-priced) idle-address SKU
    both contain the "PublicIPv4" substring but aren't the per-EIP rate."""
    fake_pricing([
        _product(None, {"usagetype": "PublicIPv4:ContiguousBlock"}, _dim(0.01)),
        _product(None, {"usagetype": "PublicIPv4:IdleAddress"}, _dim(0.005)),
    ])
    assert fetcher.fetch_eip("us-east-1", db=tmp_db) == 0
    assert _keys("AmazonVPC", "us-east-1", tmp_db) == {}


# ── CloudTrail ───────────────────────────────────────────────────────────────


def test_fetch_cloudtrail_management_data_and_insights(fake_pricing, tmp_db):
    fake_pricing([
        _product("", {"usagetype": "PaidEventsRecorded"}, _dim(2.00, unit="Events")),
        _product("", {"usagetype": "DataEventsRecorded"}, _dim(0.10, unit="Events")),
        _product("", {"usagetype": "InsightsEventsRecorded"}, _dim(0.35, unit="Events")),
    ])
    assert fetcher.fetch_cloudtrail("us-east-1", db=tmp_db) == 3
    assert _keys("AWSCloudTrail", "us-east-1", tmp_db) == {
        "cloudtrail:management": pytest.approx(2.00),
        "cloudtrail:data": pytest.approx(0.10),
        "cloudtrail:insights": pytest.approx(0.35),
    }


# ── GuardDuty ────────────────────────────────────────────────────────────────


def test_fetch_guardduty_analysis_rate(fake_pricing, tmp_db):
    fake_pricing([
        _product("", {"usagetype": "CloudTrailEvents"}, _dim(4.00, unit="GB")),
    ])
    assert fetcher.fetch_guardduty("us-east-1", db=tmp_db) == 1
    assert _keys("AmazonGuardDuty", "us-east-1", tmp_db) == {"guardduty:analysis": pytest.approx(4.00)}


# ── DocumentDB ───────────────────────────────────────────────────────────────


def test_fetch_docdb_instance_price(fake_pricing, tmp_db):
    fake_pricing([
        _product("Database Instance", {"instanceType": "db.r5.large"}, _dim(0.277)),
    ])
    assert fetcher.fetch_docdb("us-east-1", db=tmp_db) == 1
    assert _keys("AmazonDocDB", "us-east-1", tmp_db) == {"docdb:db.r5.large": pytest.approx(0.277)}


# ── FSx for Windows ──────────────────────────────────────────────────────────


def test_fetch_fsx_windows_storage_and_throughput(fake_pricing, tmp_db):
    fake_pricing([
        _product("Storage", {"storageMedia": "SSD"}, _dim(0.13, unit="GB-Mo")),
        _product("Storage", {"storageMedia": "HDD"}, _dim(0.013, unit="GB-Mo")),
        _product("Provisioned Throughput", {}, _dim(2.20, unit="MBps-Mo")),
    ])
    assert fetcher.fetch_fsx_windows("us-east-1", db=tmp_db) == 3
    assert _keys("AmazonFSx", "us-east-1", tmp_db) == {
        "fsx:windows:storage:ssd": pytest.approx(0.13),
        "fsx:windows:storage:hdd": pytest.approx(0.013),
        "fsx:windows:throughput": pytest.approx(2.20),
    }


# ── ACM Private CA ───────────────────────────────────────────────────────────


def test_fetch_acmpca_general_purpose_short_lived_and_certificates(fake_pricing, tmp_db):
    fake_pricing([
        _product("", {"usagetype": "PrivateCertificateAuthority"}, _dim(400.00, unit="Mo")),
        _product("", {"usagetype": "ShortLivedPrivateCertificateAuthority"}, _dim(50.00, unit="Mo")),
        _product("", {"usagetype": "CertificatesIssued"}, _dim(0.75, unit="Certificates")),
    ])
    assert fetcher.fetch_acmpca("us-east-1", db=tmp_db) == 3
    assert _keys("AWSCertificateManager", "us-east-1", tmp_db) == {
        "acmpca:monthly:general_purpose": pytest.approx(400.00),
        "acmpca:monthly:short_lived": pytest.approx(50.00),
        "acmpca:certificate": pytest.approx(0.75),
    }


# ── Athena ───────────────────────────────────────────────────────────────────


def test_fetch_athena_scanned_price(fake_pricing, tmp_db):
    fake_pricing([
        _product("Amazon Athena", {}, _dim(5.00, unit="TB")),
    ])
    assert fetcher.fetch_athena("us-east-1", db=tmp_db) == 1
    assert _keys("AmazonAthena", "us-east-1", tmp_db) == {"athena:scanned": pytest.approx(5.00)}


# ── FSx for Lustre ───────────────────────────────────────────────────────────


def test_fetch_fsx_lustre_storage_by_deployment_and_media(fake_pricing, tmp_db):
    fake_pricing([
        _product("Storage", {"deploymentOption": "Scratch2", "storageMedia": "SSD"}, _dim(0.14, unit="GB-Mo")),
        _product("Storage", {"deploymentOption": "Persistent1", "storageMedia": "HDD"}, _dim(0.025, unit="GB-Mo")),
    ])
    assert fetcher.fetch_fsx_lustre("us-east-1", db=tmp_db) == 2
    assert _keys("AmazonFSx", "us-east-1", tmp_db) == {
        "fsx:lustre:SCRATCH2:SSD": pytest.approx(0.14),
        "fsx:lustre:PERSISTENT1:HDD": pytest.approx(0.025),
    }


# ── Neptune ──────────────────────────────────────────────────────────────────


def test_fetch_neptune_instance_price(fake_pricing, tmp_db):
    fake_pricing([
        _product("Database Instance", {"instanceType": "db.r5.large"}, _dim(0.348)),
    ])
    assert fetcher.fetch_neptune("us-east-1", db=tmp_db) == 1
    assert _keys("AmazonNeptune", "us-east-1", tmp_db) == {"neptune:db.r5.large": pytest.approx(0.348)}


# ── Global Accelerator ───────────────────────────────────────────────────────


def test_fetch_global_accelerator_fixed_fee_and_data_premium(fake_pricing, tmp_db):
    fake_pricing([
        _product("AWS Global Accelerator", {"usagetype": "Global-Accelerator-fixed-fee"}, _dim(0.025)),
        _product("AWS Global Accelerator", {"usagetype": "AU-AU-OUT-Bytes-AWS"}, _dim(0.015, unit="GB")),
    ])
    assert fetcher.fetch_global_accelerator("us-east-1", db=tmp_db) == 2
    assert _keys("AWSGlobalAccelerator", "us-east-1", tmp_db) == {
        "globalaccelerator:hourly": pytest.approx(0.025),
        "globalaccelerator:data": pytest.approx(0.015),
    }


# ── Amazon MQ ────────────────────────────────────────────────────────────────


def test_fetch_mq_broker_and_storage(fake_pricing, tmp_db):
    fake_pricing([
        _product("Broker Instances", {"instanceType": "mq.m5.large"}, _dim(0.30)),
        _product("Storage", {}, _dim(0.30, unit="GB-Mo")),
    ])
    assert fetcher.fetch_mq("us-east-1", db=tmp_db) == 2
    assert _keys("AmazonMQ", "us-east-1", tmp_db) == {
        "mq:mq.m5.large": pytest.approx(0.30),
        "mq:storage": pytest.approx(0.30),
    }


def test_fetch_vpn_sitetosite_and_clientvpn(fake_pricing, tmp_db):
    fake_pricing([
        _product("Cloud Connectivity", {"usagetype": "VPN-Usage:Hrs"}, _dim(0.05)),
        _product("Client VPN", {"usagetype": "USE1-ClientVPN-EndpointHours"}, _dim(0.10)),
        _product("Client VPN", {"usagetype": "USE1-ClientVPN-ConnectionHours"}, _dim(0.05)),
    ])
    assert fetcher.fetch_vpn("us-east-1", db=tmp_db) == 3
    assert _keys("AmazonVPC", "us-east-1", tmp_db) == {
        "vpn:sitetosite:hourly": pytest.approx(0.05),
        "vpn:clientvpn:association:hourly": pytest.approx(0.10),
        "vpn:clientvpn:connection:hourly": pytest.approx(0.05),
    }


def test_fetch_vpn_rejects_unrelated_usagetype(fake_pricing, tmp_db):
    fake_pricing([
        _product("Cloud Connectivity", {"usagetype": "PublicIP-In"}, _dim(0.01)),
    ])
    assert fetcher.fetch_vpn("us-east-1", db=tmp_db) == 0


def test_fetch_direct_connect_by_port_speed(fake_pricing, tmp_db):
    fake_pricing([
        _product("Direct Connect Port", {"portSpeed": "1Gbps"}, _dim(0.30)),
        _product("Direct Connect Port", {"portSpeed": "10Gbps"}, _dim(2.25)),
    ])
    assert fetcher.fetch_direct_connect("us-east-1", db=tmp_db) == 2
    assert _keys("AWSDirectConnect", "us-east-1", tmp_db) == {
        "directconnect:port:1gbps": pytest.approx(0.30),
        "directconnect:port:10gbps": pytest.approx(2.25),
    }


def test_fetch_appsync_requests_and_connection_minutes(fake_pricing, tmp_db):
    fake_pricing([
        _product("API Calls", {"usagetype": "GraphQLInvocation"}, _dim(4.0, unit="requests")),
        _product("RealTime", {"usagetype": "ConnectionDuration"}, _dim(0.00002, unit="minutes")),
    ])
    assert fetcher.fetch_appsync("us-east-1", db=tmp_db) == 2
    assert _keys("AWSAppSync", "us-east-1", tmp_db) == {
        "appsync:requests": pytest.approx(4.0),
        "appsync:connectionminutes": pytest.approx(0.00002),
    }


def test_fetch_appsync_ignores_event_api_connection_duration(fake_pricing, tmp_db):
    """A distinct newer API type (`EventAPI-ConnectionDuration`) shares the
    "ConnectionDuration" suffix but isn't the GraphQL API's own rate."""
    fake_pricing([
        _product("RealTime", {"usagetype": "EventAPI-ConnectionDuration"}, _dim(0.00002, unit="minutes")),
    ])
    assert fetcher.fetch_appsync("us-east-1", db=tmp_db) == 0


def test_fetch_cognito_mau_tier(fake_pricing, tmp_db):
    fake_pricing([
        _product("User Pool MAU", {"usagetype": "CognitoUserPoolsMAU"}, _dim(0.0055, unit="users")),
    ])
    assert fetcher.fetch_cognito("us-east-1", db=tmp_db) == 1
    assert _keys("AmazonCognito", "us-east-1", tmp_db) == {
        "cognito:mau": pytest.approx(0.0055),
    }


def test_fetch_cognito_ignores_other_feature_plan_mau(fake_pricing, tmp_db):
    """Lite/Essentials/Plus/Enterprise each have their own MAU usagetype;
    only the plain default-plan rate is fetched."""
    fake_pricing([
        _product("Amazon Cognito - Plus", {"usagetype": "CognitoPlusMAU"}, _dim(0.015, unit="users")),
    ])
    assert fetcher.fetch_cognito("us-east-1", db=tmp_db) == 0


def test_fetch_glue_dpuhour(fake_pricing, tmp_db):
    fake_pricing([
        _product("AWS-Glue", {"usagetype": "DPU-Hour"}, _dim(0.44)),
    ])
    assert fetcher.fetch_glue("us-east-1", db=tmp_db) == 1
    assert _keys("AWSGlue", "us-east-1", tmp_db) == {"glue:dpuhour": pytest.approx(0.44)}


def test_fetch_sagemaker_by_instance_type(fake_pricing, tmp_db):
    fake_pricing([
        _product("ML Instance", {"instanceType": "ml.t3.medium"}, _dim(0.0582)),
        _product("ML Instance", {"instanceType": "ml.m5.xlarge"}, _dim(0.269)),
    ])
    assert fetcher.fetch_sagemaker("us-east-1", db=tmp_db) == 2
    assert _keys("AmazonSageMaker", "us-east-1", tmp_db) == {
        "sagemaker:ml.t3.medium": pytest.approx(0.0582),
        "sagemaker:ml.m5.xlarge": pytest.approx(0.269),
    }


def test_fetch_cloudhsm_flat_hourly(fake_pricing, tmp_db):
    fake_pricing([
        _product("Dedicated-Host", {"usagetype": "CloudHSMv2Usage"}, _dim(1.60)),
    ])
    assert fetcher.fetch_cloudhsm("us-east-1", db=tmp_db) == 1
    assert _keys("CloudHSM", "us-east-1", tmp_db) == {"cloudhsm:hourly": pytest.approx(1.60)}


def test_fetch_cloudhsm_ignores_hardware_variant_and_upfront(fake_pricing, tmp_db):
    fake_pricing([
        _product("Dedicated-Host", {"usagetype": "CloudHSMv2Usage-hsm2m.m"}, _dim(2.20)),
        _product("Dedicated-Host", {"usagetype": "CloudHSMv2Upfront"}, _dim(0.0)),
    ])
    assert fetcher.fetch_cloudhsm("us-east-1", db=tmp_db) == 0
    assert _keys("CloudHSM", "us-east-1", tmp_db) == {}


def test_fetch_macie_per_gb(fake_pricing, tmp_db):
    fake_pricing([
        _product("Amazon Macie", {"usagetype": "S3ContentClassification"}, _dim(1.00, unit="GB")),
    ])
    assert fetcher.fetch_macie("us-east-1", db=tmp_db) == 1
    assert _keys("AmazonMacie", "us-east-1", tmp_db) == {"macie:gb": pytest.approx(1.00)}


def test_fetch_inspector_by_resource_type(fake_pricing, tmp_db):
    fake_pricing([
        _product("Vulnerability Scanning", {"usagetype": "InspectorV2-EC2-InstanceHours"}, _dim(0.01)),
        _product("Vulnerability Scanning", {"usagetype": "InspectorV2-ECR-ImageScans"}, _dim(0.09)),
        _product("Vulnerability Scanning", {"usagetype": "InspectorV2-Lambda-FunctionHours"}, _dim(0.30)),
    ])
    assert fetcher.fetch_inspector("us-east-1", db=tmp_db) == 3
    assert _keys("AmazonInspectorV2", "us-east-1", tmp_db) == {
        "inspector:ec2": pytest.approx(0.01),
        "inspector:ecr": pytest.approx(0.09),
        "inspector:lambda": pytest.approx(0.30),
    }


# ── fetch_all ────────────────────────────────────────────────────────────────


def test_all_services_matches_fetcher_registry():
    assert set(fetcher.ALL_SERVICES) == {
        "ECS", "Lambda", "EC2", "EBS", "RDS", "ElastiCache", "S3", "SQS", "CloudWatch", "ELB",
        "SecretsManager", "Route53", "KMS", "WAF", "DataTransfer", "NATGateway", "Config",
        "EKS", "DynamoDB", "VPCEndpoint", "SNS", "EFS", "ECR", "APIGateway", "CloudFront",
        "Kinesis", "StepFunctions", "EventBridge", "TransitGateway", "S3Files", "OpenSearch",
        "Redshift", "Backup", "MSK", "EIP", "CloudTrail", "GuardDuty", "DocDB", "FSxWindows",
        "ACMPCA", "Athena", "FSxLustre", "Neptune", "GlobalAccelerator", "MQ",
        "VPN", "DirectConnect", "AppSync", "Cognito", "Glue", "SageMaker", "CloudHSM",
        "Macie", "Inspector",
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
    assert _keys("AWSDataTransfer", "us-east-1", tmp_db) == {
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
    assert _keys("AWSDataTransfer", "us-east-1", tmp_db) == {
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


# ── AWS Config ───────────────────────────────────────────────────────────────


def test_fetch_config_item_and_rule_evaluation(fake_pricing, tmp_db):
    fake_pricing([
        _product("Management Tools", {"usagetype": "USE1-ConfigurationItemRecorded"},
                 _dim(0.003, unit="items")),
        _product("Management Tools", {"usagetype": "USE1-ConfigRuleEvaluations"}, [
            _dim(0.001, unit="evaluations", begin_range="0"),
            _dim(0.0008, unit="evaluations", begin_range="100000"),
            _dim(0.0005, unit="evaluations", begin_range="500000"),
        ]),
    ])
    assert fetcher.fetch_config("us-east-1", db=tmp_db) == 2
    assert _keys("AWSConfig", "us-east-1", tmp_db) == {
        "config:item": pytest.approx(0.003),
        "config:rule:evaluation": pytest.approx(0.001),
    }


def test_fetch_config_ignores_unrelated_usagetype(fake_pricing, tmp_db):
    fake_pricing([
        _product("Management Tools", {"usagetype": "USE1-SomethingElse"}, _dim(0.5)),
    ])
    assert fetcher.fetch_config("us-east-1", db=tmp_db) == 0
