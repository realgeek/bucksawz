"""
AWS Pricing API fetchers. Writes results to the local SQLite price cache.

The Pricing API endpoint is global (us-east-1 only). Prices are per AWS region.
Older services filter by `location` display name; newer ones accept `regionCode`.
"""
from __future__ import annotations
import json
from typing import Iterator, Optional
from pathlib import Path
import boto3
from . import db as price_db

# AWS Pricing API uses display names for region, not codes.
_REGION_DISPLAY: dict[str, str] = {
    "af-south-1": "Africa (Cape Town)",
    "ap-east-1": "Asia Pacific (Hong Kong)",
    "ap-northeast-1": "Asia Pacific (Tokyo)",
    "ap-northeast-2": "Asia Pacific (Seoul)",
    "ap-northeast-3": "Asia Pacific (Osaka)",
    "ap-south-1": "Asia Pacific (Mumbai)",
    "ap-southeast-1": "Asia Pacific (Singapore)",
    "ap-southeast-2": "Asia Pacific (Sydney)",
    "ap-southeast-3": "Asia Pacific (Jakarta)",
    "ca-central-1": "Canada (Central)",
    "eu-central-1": "EU (Frankfurt)",
    "eu-north-1": "EU (Stockholm)",
    "eu-south-1": "Europe (Milan)",
    "eu-west-1": "EU (Ireland)",
    "eu-west-2": "EU (London)",
    "eu-west-3": "EU (Paris)",
    "me-south-1": "Middle East (Bahrain)",
    "sa-east-1": "South America (Sao Paulo)",
    "us-east-1": "US East (N. Virginia)",
    "us-east-2": "US East (Ohio)",
    "us-west-1": "US West (N. California)",
    "us-west-2": "US West (Oregon)",
}


def region_display(code: str) -> str:
    return _REGION_DISPLAY.get(code, code)


def _pricing_client(profile: Optional[str] = None):
    """Pricing API is only accessible from us-east-1."""
    session = boto3.Session(profile_name=profile, region_name="us-east-1")
    return session.client("pricing")


def _iter_products(pricing, service_code: str, filters: list[dict]) -> Iterator[dict]:
    paginator = pricing.get_paginator("get_products")
    for page in paginator.paginate(ServiceCode=service_code, Filters=filters):
        for raw in page.get("PriceList", []):
            try:
                yield json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue


def _ondemand_price(product: dict) -> Optional[tuple[str, float, str]]:
    """Extract (unit, price_usd, description) from the first on-demand dimension with price > 0."""
    for offer in product.get("terms", {}).get("OnDemand", {}).values():
        for dim in offer.get("priceDimensions", {}).values():
            price_str = dim.get("pricePerUnit", {}).get("USD", "0")
            unit = dim.get("unit", "")
            desc = dim.get("description", "")
            try:
                price = float(price_str)
                if price > 0:
                    return unit, price, desc
            except (ValueError, TypeError):
                continue
    return None


def fetch_fargate(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    ECS Fargate vCPU-hour and GB-hour prices for `region`.
    Service code: AmazonECS, product family: Compute.
    Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [
        {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
    ]
    stored = 0
    for product in _iter_products(pricing, "AmazonECS", filters):
        attrs = product.get("product", {}).get("attributes", {})
        family = product.get("product", {}).get("productFamily", "")
        if "Compute" not in family:
            continue
        usagetype = attrs.get("usagetype", "")
        # Fargate Linux/x86 line items carry no operatingSystem attribute at
        # all (only the Windows variants do), so filter on usagetype instead.
        if "Fargate" not in usagetype or "Windows" in usagetype:
            continue
        is_arm = "ARM" in usagetype
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        if "vCPU-Hours" in usagetype:
            key = "fargate:vcpu:arm" if is_arm else "fargate:vcpu"
        elif "GB-Hours" in usagetype and "Ephemeral" not in usagetype:
            key = "fargate:memory:arm" if is_arm else "fargate:memory"
        else:
            continue
        price_db.upsert("AmazonECS", region, key, unit, price, desc, db=db)
        stored += 1
    return stored


# Matched exactly, not by substring: "AWS-Lambda-Storage-Duration" (ephemeral
# storage GB-seconds), "AWS-Lambda-Edge-Duration" and the provisioned-concurrency
# groups all contain "duration" and would otherwise overwrite the real compute
# rate with an unrelated, much cheaper one. Same for "AWS-Lambda-Edge-Requests".
_LAMBDA_REQUEST_GROUPS = {"aws-lambda-requests", "aws-lambda-requests-arm"}
_LAMBDA_DURATION_GROUPS = {"aws-lambda-duration", "aws-lambda-duration-arm"}


def fetch_lambda(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    Lambda request and duration (GB-second) prices for `region`.
    Service code: AWSLambda.
    Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [
        {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
    ]
    stored = 0
    for product in _iter_products(pricing, "AWSLambda", filters):
        attrs = product.get("product", {}).get("attributes", {})
        group = attrs.get("group", "").lower()
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        if group in _LAMBDA_REQUEST_GROUPS:
            key = "lambda:requests"
        elif group in _LAMBDA_DURATION_GROUPS:
            arch = attrs.get("processorArchitecture", "x86_64").replace(" ", "_").lower()
            key = f"lambda:duration:{arch}"
        else:
            continue
        price_db.upsert("AWSLambda", region, key, unit, price, desc, db=db)
        stored += 1
    return stored


def fetch_ec2_instances(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    EC2 on-demand Linux/UNIX shared-tenancy instance prices for `region`.
    Service code: AmazonEC2, product family: Compute Instance.
    Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    location = region_display(region)
    filters = [
        {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": "Linux"},
        {"Type": "TERM_MATCH", "Field": "tenancy", "Value": "Shared"},
        {"Type": "TERM_MATCH", "Field": "preInstalledSw", "Value": "NA"},
        {"Type": "TERM_MATCH", "Field": "capacitystatus", "Value": "Used"},
        {"Type": "TERM_MATCH", "Field": "location", "Value": location},
    ]
    stored = 0
    for product in _iter_products(pricing, "AmazonEC2", filters):
        family = product.get("product", {}).get("productFamily", "")
        if "Compute Instance" not in family:
            continue
        instance_type = product.get("product", {}).get("attributes", {}).get("instanceType", "")
        if not instance_type:
            continue
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        key = f"ec2:{instance_type}:linux:shared"
        price_db.upsert("AmazonEC2", region, key, unit, price, desc, db=db)
        stored += 1
    return stored


def fetch_rds_instances(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    RDS on-demand instance prices for MySQL, PostgreSQL, Aurora MySQL, Aurora PostgreSQL.
    Service code: AmazonRDS, product family: Database Instance.
    Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    location = region_display(region)
    filters = [
        {"Type": "TERM_MATCH", "Field": "location", "Value": location},
    ]
    _ENGINES = {"MySQL", "PostgreSQL", "Aurora MySQL", "Aurora PostgreSQL"}
    stored = 0
    for product in _iter_products(pricing, "AmazonRDS", filters):
        family = product.get("product", {}).get("productFamily", "")
        if "Database Instance" not in family:
            continue
        attrs = product.get("product", {}).get("attributes", {})
        instance_type = attrs.get("instanceType", "")
        engine = attrs.get("databaseEngine", "")
        deployment = attrs.get("deploymentOption", "Single-AZ")
        if not instance_type or engine not in _ENGINES:
            continue
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        key = f"rds:{instance_type}:{engine}:{deployment}"
        price_db.upsert("AmazonRDS", region, key, unit, price, desc, db=db)
        stored += 1
    return stored


def fetch_elasticache(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    ElastiCache on-demand node prices for `region`.
    Service code: AmazonElastiCache, product family: Cache Instance.
    Excludes Extended Support / Sync Durability surcharge line items — those
    are additive charges on top of the base NodeUsage rate, not a distinct node price.
    Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [
        {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Cache Instance"},
    ]
    stored = 0
    for product in _iter_products(pricing, "AmazonElastiCache", filters):
        attrs = product.get("product", {}).get("attributes", {})
        usagetype = attrs.get("usagetype", "")
        instance_type = attrs.get("instanceType", "")
        engine = attrs.get("cacheEngine", "")
        if "NodeUsage:" not in usagetype or "ExtendedSupport" in usagetype or "SyncDurability" in usagetype:
            continue
        if not instance_type or not engine:
            continue
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        key = f"elasticache:{instance_type}:{engine.lower()}"
        price_db.upsert("AmazonElastiCache", region, key, unit, price, desc, db=db)
        stored += 1
    return stored


_S3_STORAGE_CLASSES = {
    "Standard": "standard",
    "Standard - Infrequent Access": "standard_ia",
    "One Zone - Infrequent Access": "one_zone_ia",
    "Glacier Instant Retrieval": "glacier_instant_retrieval",
    "Amazon Glacier": "glacier_flexible_retrieval",
    "Intelligent-Tiering Frequent Access": "intelligent_tiering",
    "Reduced Redundancy": "reduced_redundancy",
    "Express One Zone": "express_one_zone",
}


def fetch_s3(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    S3 per-GB monthly storage prices for `region`, one row per storage class in
    `_S3_STORAGE_CLASSES`. Keyed on `volumeType` (not `storageClass`, which is a
    coarser display grouping shared across several classes). Uses the first-tier
    price since Standard storage is priced in declining GB tiers. Excludes Deep
    Archive and the granular Intelligent-Tiering access-tier line items, whose
    volumeType naming was ambiguous in a spot-check of the Pricing API response.
    Service code: AmazonS3, product family: Storage.
    Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [
        {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Storage"},
    ]
    stored = 0
    for product in _iter_products(pricing, "AmazonS3", filters):
        attrs = product.get("product", {}).get("attributes", {})
        volume_type = attrs.get("volumeType", "")
        slug = _S3_STORAGE_CLASSES.get(volume_type)
        if slug is None:
            continue
        result = _first_tier_price(product)
        if result is None:
            continue
        unit, price, desc = result
        key = f"s3:storage:{slug}"
        price_db.upsert("AmazonS3", region, key, unit, price, desc, db=db)
        stored += 1
    return stored


_SQS_QUEUE_TYPES = {
    "Standard": "standard",
    "FIFO (first-in, first-out)": "fifo",
    "Fair": "fair",
}


def fetch_sqs(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    SQS per-request prices for `region`, one row per queue type.
    Service code: AWSQueueService, product family: API Request.
    Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [
        {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "API Request"},
    ]
    stored = 0
    for product in _iter_products(pricing, "AWSQueueService", filters):
        attrs = product.get("product", {}).get("attributes", {})
        queue_type = attrs.get("queueType", "")
        slug = _SQS_QUEUE_TYPES.get(queue_type)
        if slug is None:
            continue
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        key = f"sqs:requests:{slug}"
        price_db.upsert("AWSQueueService", region, key, unit, price, desc, db=db)
        stored += 1
    return stored


def _all_tier_prices(product: dict) -> list[tuple[int, str, float, str]]:
    """
    Like _first_tier_price, but returns every priced tier as
    (begin_range_gb, unit, price_usd, description), sorted ascending —
    for pricing where every threshold matters (data transfer) rather than
    just the cheapest/first one.
    """
    tiers: list[tuple[int, str, float, str]] = []
    for offer in product.get("terms", {}).get("OnDemand", {}).values():
        for dim in offer.get("priceDimensions", {}).values():
            price_str = dim.get("pricePerUnit", {}).get("USD", "0")
            try:
                price = float(price_str)
            except (ValueError, TypeError):
                continue
            if price <= 0:
                continue
            try:
                begin_gb = int(dim.get("beginRange", "0"))
            except (ValueError, TypeError):
                begin_gb = 0
            tiers.append((begin_gb, dim.get("unit", ""), price, dim.get("description", "")))
    tiers.sort(key=lambda t: t[0])
    return tiers


def _first_tier_price(product: dict) -> Optional[tuple[str, float, str]]:
    """Like _ondemand_price, but prefers the beginRange=='0' tier for tiered pricing
    (e.g. CloudWatch custom metrics get cheaper per-metric past 10k/240k/750k/1M)."""
    for offer in product.get("terms", {}).get("OnDemand", {}).values():
        dims = list(offer.get("priceDimensions", {}).values())
        dims.sort(key=lambda d: d.get("beginRange", "") != "0")
        for dim in dims:
            price_str = dim.get("pricePerUnit", {}).get("USD", "0")
            unit = dim.get("unit", "")
            desc = dim.get("description", "")
            try:
                price = float(price_str)
                if price > 0:
                    return unit, price, desc
            except (ValueError, TypeError):
                continue
    return None


def fetch_cloudwatch(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    A handful of baseline CloudWatch prices for `region`: alarms, custom metrics
    (first-tier rate), and Logs ingestion/storage. CloudWatch has dozens of niche
    usage types (RUM, Synthetics, Contributor Insights, OTEL, etc.) not covered here.
    Service code: AmazonCloudWatch.
    Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [{"Type": "TERM_MATCH", "Field": "regionCode", "Value": region}]
    stored = 0
    for product in _iter_products(pricing, "AmazonCloudWatch", filters):
        attrs = product.get("product", {}).get("attributes", {})
        usagetype = attrs.get("usagetype", "")
        group = attrs.get("group", "")

        if usagetype.endswith("CW:AlarmMonitorUsage"):
            key, unit_price = "cloudwatch:alarm", _ondemand_price(product)
        elif usagetype.endswith("CW:MetricMonitorUsage"):
            key, unit_price = "cloudwatch:metric", _first_tier_price(product)
        elif usagetype.endswith("DataProcessing-Bytes") and group == "Ingested Logs":
            key, unit_price = "cloudwatch:logs:ingestion", _ondemand_price(product)
        elif usagetype.endswith("TimedStorage-ByteHrs") and group == "":
            fam = product.get("product", {}).get("productFamily", "")
            if fam != "Storage Snapshot":
                continue
            key, unit_price = "cloudwatch:logs:storage", _ondemand_price(product)
        else:
            continue

        if unit_price is None:
            continue
        unit, price, desc = unit_price
        price_db.upsert("AmazonCloudWatch", region, key, unit, price, desc, db=db)
        stored += 1
    return stored


_ELB_TYPES = {
    "Load Balancer-Application": "application",
    "Load Balancer-Network": "network",
    "Load Balancer-Gateway": "gateway",
    "Load Balancer": "classic",
}


def fetch_elb(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    ELB hourly + LCU prices for `region`, one pair per load balancer type
    (application/network/gateway/classic).
    Service code: AWSELB.
    Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [{"Type": "TERM_MATCH", "Field": "regionCode", "Value": region}]
    stored = 0
    for product in _iter_products(pricing, "AWSELB", filters):
        attrs = product.get("product", {}).get("attributes", {})
        family = product.get("product", {}).get("productFamily", "")
        usagetype = attrs.get("usagetype", "")
        lb_slug = _ELB_TYPES.get(family)
        if lb_slug is None:
            continue
        # "Outposts-" and "TS-" (Trust Store) usage types also end with these
        # suffixes and would otherwise silently overwrite the real regional price.
        if "Outposts" in usagetype or usagetype.startswith("TS-"):
            continue

        if usagetype.endswith("LoadBalancerUsage") and not usagetype.endswith("Reserved LoadBalancerUsage"):
            key = f"elb:hourly:{lb_slug}"
        elif usagetype.endswith("LCUUsage") and not usagetype.endswith("ReservedLCUUsage"):
            key = f"elb:lcu:{lb_slug}"
        elif lb_slug == "classic" and usagetype.endswith("DataProcessing-Bytes"):
            key = "elb:data:classic"
        else:
            continue

        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        price_db.upsert("AWSELB", region, key, unit, price, desc, db=db)
        stored += 1
    return stored


# volumeApiName -> the same slug Terraform's aws_ebs_volume `type` uses.
_EBS_VOLUME_TYPES = {"standard", "gp2", "gp3", "io1", "io2", "st1", "sc1"}


def fetch_ebs(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    EBS per-GB-month storage for all seven volume types, plus the provisioned
    IOPS and throughput rates that sit on top of storage for io1/io2/gp3.

    gp3 bills IOPS and throughput only above its included baseline (3,000 IOPS /
    125 MiB/s) — the pricer applies that baseline, not this fetcher, since it's
    a property of how the volume is billed rather than of the price itself.
    io1 has no free IOPS tier. io2 IOPS is billed in three tiers (0-32,000 /
    32,001-64,000 / 64,001+); the tier boundary is fixed by AWS, not fetched.

    Service code: AmazonEC2, product families: Storage, System Operation,
    Provisioned Throughput. Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    stored = 0

    filters = [
        {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Storage"},
    ]
    for product in _iter_products(pricing, "AmazonEC2", filters):
        attrs = product.get("product", {}).get("attributes", {})
        vol = attrs.get("volumeApiName", "")
        if vol not in _EBS_VOLUME_TYPES:
            continue
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        price_db.upsert("AmazonEC2", region, f"ebs:storage:{vol}", unit, price, desc, db=db)
        stored += 1

    filters = [
        {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "System Operation"},
    ]
    for product in _iter_products(pricing, "AmazonEC2", filters):
        attrs = product.get("product", {}).get("attributes", {})
        usagetype = attrs.get("usagetype", "")
        # Bare suffix (no .tierN) is io2's first tier, 0-32,000 IOPS.
        if usagetype.endswith("EBS:VolumeP-IOPS.gp3"):
            key = "ebs:iops:gp3"
        elif usagetype.endswith("EBS:VolumeP-IOPS.piops"):
            key = "ebs:iops:io1"
        elif usagetype.endswith("EBS:VolumeP-IOPS.io2"):
            key = "ebs:iops:io2:tier1"
        elif usagetype.endswith("EBS:VolumeP-IOPS.io2.tier2"):
            key = "ebs:iops:io2:tier2"
        elif usagetype.endswith("EBS:VolumeP-IOPS.io2.tier3"):
            key = "ebs:iops:io2:tier3"
        else:
            continue
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        price_db.upsert("AmazonEC2", region, key, unit, price, desc, db=db)
        stored += 1

    filters = [
        {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Provisioned Throughput"},
    ]
    for product in _iter_products(pricing, "AmazonEC2", filters):
        attrs = product.get("product", {}).get("attributes", {})
        if not attrs.get("usagetype", "").endswith("EBS:VolumeP-Throughput.gp3"):
            continue
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        # Priced per GiBps-month; the pricer works in MiB/s (1 GiBps = 1024 MiBps).
        if unit == "GiBps-mo":
            price = price / 1024.0
            unit = "MiBps-Mo"
        price_db.upsert("AmazonEC2", region, "ebs:throughput:gp3", unit, price, desc, db=db)
        stored += 1

    return stored


def fetch_secretsmanager(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    Secrets Manager per-secret monthly price and per-API-request price for `region`.
    Service code: AWSSecretsManager, product families: Secret, API Request.
    Only two line items exist per region, no contaminant filtering needed.
    Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [{"Type": "TERM_MATCH", "Field": "regionCode", "Value": region}]
    stored = 0
    for product in _iter_products(pricing, "AWSSecretsManager", filters):
        family = product.get("product", {}).get("productFamily", "")
        if family == "Secret":
            key = "secretsmanager:secret"
        elif family == "API Request":
            key = "secretsmanager:requests"
        else:
            continue
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        price_db.upsert("AWSSecretsManager", region, key, unit, price, desc, db=db)
        stored += 1
    return stored


def fetch_route53(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    Route 53 hosted zone and standard DNS query prices. Unlike every other
    fetcher here, Route 53's hosted-zone and standard-query pricing is global
    (regionCode is "", location is "Any") rather than per-region — the same
    price gets stored under whatever `region` key is requested, matching how
    `prices update` calls every fetcher once per region regardless.

    Only the first pricing tier of each is stored: $0.50/zone (of the first 25
    per account) and $0.40 per million standard queries (of the first 1
    billion/month) — same simplification as S3/CloudWatch's first-tier
    pricing, since tier occupancy depends on account-wide totals bucksawz
    can't see from a single Terraform plan.

    The "DNS Query" product family also carries Route 53 Resolver's
    region-scoped query pricing (`usagetype` prefixed with the region code,
    e.g. "USE1-DNS-Queries", no `routingType` attribute) — excluded by
    requiring the unprefixed "DNS-Queries" usagetype and routingType
    "Standard" / routingTarget "External".

    Service code: AmazonRoute53, product families: DNS Zone, DNS Query.
    Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    stored = 0

    for product in _iter_products(pricing, "AmazonRoute53", [
        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "DNS Zone"},
    ]):
        attrs = product.get("product", {}).get("attributes", {})
        if attrs.get("usagetype") != "HostedZone":
            continue
        result = _first_tier_price(product)
        if result is None:
            continue
        unit, price, desc = result
        price_db.upsert("AmazonRoute53", region, "route53:hostedzone", unit, price, desc, db=db)
        stored += 1

    for product in _iter_products(pricing, "AmazonRoute53", [
        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "DNS Query"},
    ]):
        attrs = product.get("product", {}).get("attributes", {})
        if (
            attrs.get("usagetype") != "DNS-Queries"
            or attrs.get("routingType") != "Standard"
            or attrs.get("routingTarget") != "External"
        ):
            continue
        result = _first_tier_price(product)
        if result is None:
            continue
        unit, price, desc = result
        price_db.upsert("AmazonRoute53", region, "route53:queries", unit, price, desc, db=db)
        stored += 1

    return stored


def fetch_kms(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    KMS customer-managed key monthly price and standard (symmetric) API
    request price for `region`. Excludes asymmetric/GenerateDataKeyPair
    request pricing (several times more expensive per request) by requiring
    the exact product families used only by the standard rates — those
    variants carry no productFamily at all, so a substring/usagetype-suffix
    match would risk picking one of them up instead.
    Service code: awskms, product families: Encryption Key, API Request.
    Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [{"Type": "TERM_MATCH", "Field": "regionCode", "Value": region}]
    stored = 0
    for product in _iter_products(pricing, "awskms", filters):
        family = product.get("product", {}).get("productFamily", "")
        if family == "Encryption Key":
            key = "kms:key"
        elif family == "API Request":
            key = "kms:requests"
        else:
            continue
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        price_db.upsert("awskms", region, key, unit, price, desc, db=db)
        stored += 1
    return stored


def fetch_waf(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    WAFv2 web ACL, rule, and baseline request prices for `region`. WAF's
    Pricing API response puts everything under one productFamily ("Web
    Application Firewall"), so `group` + `usagetype` do the real filtering:

    - "WebACLV2" / "RuleV2" suffixes select the v2 (aws_wafv2_web_acl) fixed
      charges, excluding the classic-WAF "WebACL"/"Rule" line items (no "V2")
      that share the same $5/$1 prices but apply to a different resource.
    - Request pricing is actually tiered by Web ACL Capacity Units (WCU),
      which depends on rule complexity bucksawz can't compute from Terraform
      config alone — "RequestV2-Tier0" (group "Request", the cheapest/base
      tier) is used as a representative rate instead, matching the flat
      $0.60/million AWS advertises on its pricing page. group == "Request"
      (not "Request (Shield Protected)") also excludes the Shield-protected
      and AMR managed-rule-group request surcharges.

    Service code: awswaf, product family: Web Application Firewall.
    Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [{"Type": "TERM_MATCH", "Field": "regionCode", "Value": region}]
    stored = 0
    for product in _iter_products(pricing, "awswaf", filters):
        attrs = product.get("product", {}).get("attributes", {})
        usagetype = attrs.get("usagetype", "")
        group = attrs.get("group", "")
        if group == "Web ACL" and usagetype.endswith("WebACLV2"):
            key = "waf:webacl"
        elif group == "Rule" and usagetype.endswith("RuleV2"):
            key = "waf:rule"
        elif group == "Request" and usagetype.endswith("RequestV2-Tier0"):
            key = "waf:requests"
        else:
            continue
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        price_db.upsert("awswaf", region, key, unit, price, desc, db=db)
        stored += 1
    return stored


def fetch_data_transfer(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    EC2 data transfer pricing for `region`: every tier of internet egress
    ("AWS Outbound") plus the flat inter-AZ rate ("IntraRegion"). Inbound
    internet transfer is $0/GB and is naturally excluded since
    `_all_tier_prices`/`_ondemand_price` drop zero-priced dimensions.

    Internet egress is billed against an account-wide cumulative tier (first
    10TB, next 40TB, next 100TB, over 150TB/month) that bucksawz has no way
    to resolve from a single Terraform plan — every tier's rate is stored
    under its own price_key (`datatransfer:out:<begin_range_gb>`) so the
    pricer can surface them all as unit-priced, no-total informational
    components rather than guessing which tier applies.

    Region-to-region ("InterRegion") transfer is deliberately excluded: it
    depends on a (from, to) region pair Terraform config can't express, and
    shares this same "Data Transfer" product family.

    Service code: AmazonEC2, product family: Data Transfer.
    Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [
        {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Data Transfer"},
    ]
    stored = 0
    for product in _iter_products(pricing, "AmazonEC2", filters):
        attrs = product.get("product", {}).get("attributes", {})
        transfer_type = attrs.get("transferType", "")
        if transfer_type == "AWS Outbound":
            for begin_gb, unit, price, desc in _all_tier_prices(product):
                price_db.upsert(
                    "AmazonEC2", region, f"datatransfer:out:{begin_gb}", unit, price, desc, db=db,
                )
                stored += 1
        elif transfer_type == "IntraRegion":
            result = _ondemand_price(product)
            if result is None:
                continue
            unit, price, desc = result
            price_db.upsert("AmazonEC2", region, "datatransfer:regional", unit, price, desc, db=db)
            stored += 1
    return stored


def fetch_nat_gateway(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    NAT Gateway hourly + per-GB data-processed prices for `region`. Both are
    flat rates — no tiers, no config-dependent variants — same shape as
    ELB's hourly+LCU pair.

    Service code: AmazonEC2, product family: NAT Gateway. usagetype suffixes
    are "NatGateway-Hours" and "NatGateway-Bytes"; Outposts carries its own
    NAT Gateway line items under the same family with an "Outposts" usagetype
    prefix, excluded the same way fetch_elb excludes Outposts ELB pricing.
    Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [
        {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "NAT Gateway"},
    ]
    stored = 0
    for product in _iter_products(pricing, "AmazonEC2", filters):
        attrs = product.get("product", {}).get("attributes", {})
        usagetype = attrs.get("usagetype", "")
        if "Outposts" in usagetype:
            continue
        if usagetype.endswith("NatGateway-Hours"):
            key = "natgateway:hourly"
        elif usagetype.endswith("NatGateway-Bytes"):
            key = "natgateway:data"
        else:
            continue
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        price_db.upsert("AmazonEC2", region, key, unit, price, desc, db=db)
        stored += 1
    return stored


def fetch_config(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    AWS Config configuration-item and rule-evaluation prices for `region`.
    Both are driven entirely by usage (how often resources change, how many
    evaluations rules run) that a single Terraform plan can't resolve —
    see `_price_config_recorder`/`_price_config_rule` in pricer.py, which
    store these as unit-priced, no-total components, same as S3/SQS.

    Configuration items are a flat per-item rate. Rule evaluations are
    tiered by account-wide monthly volume (first 100K / next 400K / over
    500K); only the first tier is stored, same simplification as
    S3/CloudWatch/Route53's first-tier pricing.

    NOTE: written without live Pricing API access (no AWS credentials in
    the dev sandbox) — the exact `usagetype` substrings below are inferred
    from AWS's public Config pricing page, not verified against a real
    `get_products` response. Run `bucksawz prices update --services Config`
    and check `bucksawz prices info` against the console's advertised
    rates before trusting this in production; adjust the substrings here
    if they don't match.

    Service code: AWSConfig. Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [{"Type": "TERM_MATCH", "Field": "regionCode", "Value": region}]
    stored = 0
    for product in _iter_products(pricing, "AWSConfig", filters):
        attrs = product.get("product", {}).get("attributes", {})
        usagetype = attrs.get("usagetype", "")
        if "ConfigurationItemRecorded" in usagetype:
            result = _ondemand_price(product)
            if result is None:
                continue
            unit, price, desc = result
            price_db.upsert("AWSConfig", region, "config:item", unit, price, desc, db=db)
            stored += 1
        elif "ConfigRuleEvaluations" in usagetype:
            result = _first_tier_price(product)
            if result is None:
                continue
            unit, price, desc = result
            price_db.upsert("AWSConfig", region, "config:rule:evaluation", unit, price, desc, db=db)
            stored += 1
    return stored


def fetch_eks(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    EKS control-plane hourly price for `region`. Flat regardless of
    Kubernetes version under AWS's "Standard" support policy; only that rate
    is stored — clusters on the "Extended" support policy (older, deprecated
    k8s versions) pay several times more, but that's not derivable from a
    Terraform plan's `aws_eks_cluster` config alone, and this fetcher isn't
    able to distinguish it from the version string either. Excluded via an
    exact usagetype match: "...AmazonEKS-Hours:perCluster" (standard) vs.
    "...AmazonEKS-Hours:perClusterExtended" (extended support) share the same
    productFamily and would collide under a substring match.

    Worker capacity (EC2/Fargate) is priced separately by the existing EC2/
    Fargate fetchers and pricers — this only covers the cluster itself.

    Service code: AmazonEKS, product family: Compute. Returns count of rows
    stored (0 or 1).
    """
    pricing = _pricing_client(profile)
    filters = [
        {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Compute"},
    ]
    stored = 0
    for product in _iter_products(pricing, "AmazonEKS", filters):
        attrs = product.get("product", {}).get("attributes", {})
        usagetype = attrs.get("usagetype", "")
        if not usagetype.endswith(":perCluster"):
            continue
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        price_db.upsert("AmazonEKS", region, "eks:cluster", unit, price, desc, db=db)
        stored += 1
    return stored


def fetch_dynamodb(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    DynamoDB storage, provisioned-capacity, and on-demand request prices for
    `region`.

    - Storage (productFamily "Database Storage", usagetype suffix
      "TimedStorage-ByteHrs") is a flat per-GB-month rate, always usage-based
      since table size isn't derivable from config.
    - Provisioned capacity (productFamily "Provisioned Throughput", usagetype
      suffixes "ReadCapacityUnit-Hrs"/"WriteCapacityUnit-Hrs") is a flat
      per-RCU/WCU-hour rate; the *quantity* comes straight from the table's
      `read_capacity`/`write_capacity` config when `billing_mode` is
      "PROVISIONED", so this makes provisioned tables' base cost fully
      config-derivable, same shape as EC2 instance-hours.
    - On-demand request units (productFamily "API Request", usagetype
      suffixes "ReadRequestUnits"/"WriteRequestUnits") price per-request; the
      Pricing API dimension is per-unit, scaled by `_PER_MILLION` in
      pricer.py to match the report's request-cost convention. Volume isn't
      derivable from config (`billing_mode = "PAY_PER_REQUEST"` tables), so
      these stay usage-based like SQS/Lambda requests.

    Global Tables' replicated write capacity/request units
    ("ReplicatedWriteCapacityUnit-Hrs", "ReplicatedWriteRequestUnits", and
    their Read equivalents) share the same suffixes as the base metrics under
    a substring/endswith test — excluded by also rejecting the
    "Replicated"-prefixed usagetype. PITR backup storage
    ("TimedPITRStorage-ByteHrs") is excluded the same way from the plain
    storage usagetype. DAX is a separate service code, not reachable here.

    Service code: AmazonDynamoDB. Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [{"Type": "TERM_MATCH", "Field": "regionCode", "Value": region}]
    stored = 0

    def _exact_suffix(usagetype: str, suffix: str) -> bool:
        return usagetype.endswith(suffix) and not usagetype.endswith("Replicated" + suffix)

    for product in _iter_products(pricing, "AmazonDynamoDB", filters):
        attrs = product.get("product", {}).get("attributes", {})
        family = product.get("product", {}).get("productFamily", "")
        usagetype = attrs.get("usagetype", "")
        if family == "Database Storage" and _exact_suffix(usagetype, "TimedStorage-ByteHrs"):
            key = "dynamodb:storage"
        elif family == "Provisioned Throughput" and _exact_suffix(usagetype, "ReadCapacityUnit-Hrs"):
            key = "dynamodb:provisioned:read"
        elif family == "Provisioned Throughput" and _exact_suffix(usagetype, "WriteCapacityUnit-Hrs"):
            key = "dynamodb:provisioned:write"
        elif family == "API Request" and _exact_suffix(usagetype, "ReadRequestUnits"):
            key = "dynamodb:ondemand:read"
        elif family == "API Request" and _exact_suffix(usagetype, "WriteRequestUnits"):
            key = "dynamodb:ondemand:write"
        else:
            continue
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        price_db.upsert("AmazonDynamoDB", region, key, unit, price, desc, db=db)
        stored += 1
    return stored


def fetch_vpc_endpoint(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    VPC Interface Endpoint (PrivateLink) hourly + per-GB data-processed
    prices for `region`. Gateway endpoints (S3/DynamoDB) are free and carry
    no Pricing API line item at all, so there's nothing to fetch for them —
    only Interface endpoints show up here.

    Service code: AmazonVPC, product family: VpcEndpoint. usagetype suffixes
    are "VpcEndpoint-Hours" and "VpcEndpoint-Bytes" — same
    hourly-plus-per-GB shape as NAT Gateway. Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [
        {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "VpcEndpoint"},
    ]
    stored = 0
    for product in _iter_products(pricing, "AmazonVPC", filters):
        attrs = product.get("product", {}).get("attributes", {})
        usagetype = attrs.get("usagetype", "")
        if usagetype.endswith("VpcEndpoint-Hours"):
            key = "vpcendpoint:hourly"
        elif usagetype.endswith("VpcEndpoint-Bytes"):
            key = "vpcendpoint:data"
        else:
            continue
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        price_db.upsert("AmazonVPC", region, key, unit, price, desc, db=db)
        stored += 1
    return stored


def fetch_sns(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    SNS standard-topic per-request price for `region`. Deliveries to
    non-Lambda/SQS/HTTP endpoints (SMS, mobile push, email) are priced per
    destination type/country and aren't derivable from an `aws_sns_topic`'s
    own config (subscriptions are a separate resource with no destination
    detail at plan time), so only the flat publish/API request rate is
    fetched here — same simplification as SQS.

    FIFO topics share the "API Request" family with a different usagetype
    prefix (no "-FIFO" suffix marker on this dimension the way SQS's
    `queueType` attribute distinguishes queue types) — excluded via the
    `Requests-Tier1` usagetype used by standard topics only.

    Service code: AmazonSNS, product family: API Request. Returns count of
    rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [
        {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "API Request"},
    ]
    stored = 0
    for product in _iter_products(pricing, "AmazonSNS", filters):
        attrs = product.get("product", {}).get("attributes", {})
        usagetype = attrs.get("usagetype", "")
        if not usagetype.endswith("Requests-Tier1"):
            continue
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        price_db.upsert("AmazonSNS", region, "sns:requests", unit, price, desc, db=db)
        stored += 1
    return stored


def fetch_efs(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    EFS Standard storage price (per GB-month) for `region`. Always
    usage-based (file system size isn't in Terraform config), same as S3.
    Infrequent Access storage/retrieval, provisioned throughput, and One
    Zone storage classes are excluded — Standard, region-replicated storage
    is the common case and the only one derivable as a single flat rate
    without more config detail than `aws_efs_file_system` carries.

    Service code: AmazonEFS, product family: Storage. The Standard-storage
    usagetype has no "-IA"/"-OneZone"/"-ByteHrs-Provisioned" markers seen on
    the other storage classes/throughput modes, so requiring the absence of
    those substrings is the exclusion. Returns count of rows stored (0 or 1).
    """
    pricing = _pricing_client(profile)
    filters = [
        {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Storage"},
    ]
    stored = 0
    for product in _iter_products(pricing, "AmazonEFS", filters):
        attrs = product.get("product", {}).get("attributes", {})
        usagetype = attrs.get("usagetype", "")
        if not usagetype.endswith("TimedStorage-ByteHrs"):
            continue
        if "IA" in usagetype or "OneZone" in usagetype:
            continue
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        price_db.upsert("AmazonEFS", region, "efs:storage:standard", unit, price, desc, db=db)
        stored += 1
    return stored


def fetch_ecr(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    ECR private-repository storage price (per GB-month) for `region`. Always
    usage-based (image size isn't in Terraform config for
    `aws_ecr_repository`), same as S3/EFS. Data transfer out of ECR uses the
    standard EC2 data-transfer rates already covered by `fetch_data_transfer`,
    not a separate ECR line item.

    Service code: AmazonECR, product family: Storage. Returns count of rows
    stored (0 or 1).
    """
    pricing = _pricing_client(profile)
    filters = [
        {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Storage"},
    ]
    stored = 0
    for product in _iter_products(pricing, "AmazonECR", filters):
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        price_db.upsert("AmazonECR", region, "ecr:storage", unit, price, desc, db=db)
        stored += 1
    return stored


def fetch_apigateway(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    API Gateway per-request prices for `region`: REST APIs
    (`aws_api_gateway_rest_api`) and HTTP APIs (`aws_apigatewayv2_api` with
    `protocol_type = "HTTP"`). Both are tiered by account-wide monthly
    volume; only the first tier is stored, same simplification as
    S3/CloudWatch/Route53/Config's first-tier pricing. WebSocket APIs
    (`protocol_type = "WEBSOCKET"`) bill per-message and per-connection-minute
    instead of per-request and aren't fetched here — `_price_api_gateway_v2_api`
    leaves those unsupported.

    NOTE: written without live Pricing API access (no AWS credentials in the
    dev sandbox) — the usagetype substrings below ("ApiGatewayRequest" for
    REST, "ApiGatewayHttpApi" for HTTP) are inferred from AWS's public API
    Gateway pricing page structure, not verified against a real
    `get_products` response. Run `bucksawz prices update --services
    APIGateway` and sanity-check `bucksawz prices info` against the
    console's advertised rates before trusting it, and fix the substrings
    here if they don't match.

    Service code: AmazonApiGateway, product family: API Calls. Returns count
    of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [
        {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "API Calls"},
    ]
    stored = 0
    for product in _iter_products(pricing, "AmazonApiGateway", filters):
        attrs = product.get("product", {}).get("attributes", {})
        usagetype = attrs.get("usagetype", "")
        if "HttpApi" in usagetype:
            key = "apigateway:http:requests"
        elif "ApiGatewayRequest" in usagetype:
            key = "apigateway:rest:requests"
        else:
            continue
        result = _first_tier_price(product)
        if result is None:
            continue
        unit, price, desc = result
        price_db.upsert("AmazonApiGateway", region, key, unit, price, desc, db=db)
        stored += 1
    return stored


def fetch_cloudfront(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    CloudFront data-transfer-out and HTTPS-request prices, stored under
    whatever `region` key is requested — like Route 53, CloudFront pricing
    isn't AWS-region-scoped at all. It's priced per *edge-location group*
    (US/Canada/Europe cheapest, then progressively more expensive groups for
    South America, Japan, Australia, India, etc.) and tiered by cumulative
    monthly GB within each group. Only the US/Canada/Europe group's first
    tier is fetched — the cheapest and most common case — same
    simplification already used for S3/Route53/Config/API Gateway's
    first-tier pricing, compounded here with a geography simplification on
    top: a distribution actually serving mostly non-US/EU traffic will be
    underpriced by this.

    NOTE: written without live Pricing API access (no AWS credentials in the
    dev sandbox) — the `location`/`usagetype` matches below are inferred
    from AWS's public CloudFront pricing page structure, not verified
    against a real `get_products` response. Run `bucksawz prices update
    --services CloudFront` and sanity-check `bucksawz prices info` against
    the console's advertised rates before trusting this, and fix the
    filters here if they don't match.

    Service code: AmazonCloudFront, product families: Data Transfer,
    Request. Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    stored = 0

    for product in _iter_products(pricing, "AmazonCloudFront", [
        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Data Transfer"},
    ]):
        attrs = product.get("product", {}).get("attributes", {})
        if attrs.get("location") != "United States" or "DataTransfer-Out-Bytes" not in attrs.get("usagetype", ""):
            continue
        result = _first_tier_price(product)
        if result is None:
            continue
        unit, price, desc = result
        price_db.upsert("AmazonCloudFront", region, "cloudfront:data:out", unit, price, desc, db=db)
        stored += 1

    for product in _iter_products(pricing, "AmazonCloudFront", [
        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Request"},
    ]):
        attrs = product.get("product", {}).get("attributes", {})
        if attrs.get("location") != "United States" or "Requests-HTTPS" not in attrs.get("usagetype", ""):
            continue
        result = _first_tier_price(product)
        if result is None:
            continue
        unit, price, desc = result
        price_db.upsert("AmazonCloudFront", region, "cloudfront:requests:https", unit, price, desc, db=db)
        stored += 1

    return stored


def fetch_kinesis(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    Kinesis Data Streams provisioned-mode shard-hour and PUT-payload-unit
    prices for `region`. On-demand mode (`stream_mode_details.stream_mode =
    "ON_DEMAND"`) bills a completely different way (per-GB written/read
    capacity, no shards) and isn't fetched here — see
    `_price_kinesis_stream`, which leaves on-demand streams unsupported
    rather than guessing.

    NOTE: written without live Pricing API access (no AWS credentials in the
    dev sandbox) — the usagetype substrings below ("ShardHour",
    "PayloadUnits") are inferred from AWS's public Kinesis pricing page
    structure, not verified against a real `get_products` response. Run
    `bucksawz prices update --services Kinesis` and sanity-check `bucksawz
    prices info` against the console's advertised rates before trusting
    this, and fix the substrings here if they don't match.

    Service code: AmazonKinesis, product family: Kinesis Streams. Returns
    count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [
        {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Kinesis Streams"},
    ]
    stored = 0
    for product in _iter_products(pricing, "AmazonKinesis", filters):
        attrs = product.get("product", {}).get("attributes", {})
        usagetype = attrs.get("usagetype", "")
        if usagetype.endswith("ShardHour"):
            key = "kinesis:shard:hour"
        elif usagetype.endswith("PayloadUnits"):
            key = "kinesis:payload:units"
        else:
            continue
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        price_db.upsert("AmazonKinesis", region, key, unit, price, desc, db=db)
        stored += 1
    return stored


def fetch_stepfunctions(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    Step Functions prices for `region`: Standard workflow state transitions,
    and Express workflow requests + GB-second duration. Both workflow types
    are entirely usage-based (see `_price_sfn_state_machine`) — the volume
    of transitions/requests/duration a state machine will see isn't in its
    Terraform config, only its `type` ("STANDARD" or "EXPRESS").

    NOTE: written without live Pricing API access (no AWS credentials in the
    dev sandbox) — the usagetype substrings below are inferred from AWS's
    public Step Functions pricing page structure, not verified against a
    real `get_products` response. Run `bucksawz prices update --services
    StepFunctions` and sanity-check `bucksawz prices info` against the
    console's advertised rates before trusting this, and fix the substrings
    here if they don't match.

    Service code: AWSStepFunctions. Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [{"Type": "TERM_MATCH", "Field": "regionCode", "Value": region}]
    stored = 0
    for product in _iter_products(pricing, "AWSStepFunctions", filters):
        attrs = product.get("product", {}).get("attributes", {})
        usagetype = attrs.get("usagetype", "")
        if "StateTransition" in usagetype:
            key = "sfn:standard:transitions"
        elif "ExpressWorkflowsRequest" in usagetype:
            key = "sfn:express:requests"
        elif "ExpressWorkflowsDuration" in usagetype:
            key = "sfn:express:duration"
        else:
            continue
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        price_db.upsert("AWSStepFunctions", region, key, unit, price, desc, db=db)
        stored += 1
    return stored


def fetch_eventbridge(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    EventBridge custom-event-bus per-million-events price for `region`.
    Events published by AWS services to the default bus are free; only
    custom events published via `PutEvents` (the only cost an
    `aws_cloudwatch_event_bus` custom bus can incur) are priced, and the
    volume isn't derivable from the bus's own config.

    NOTE: written without live Pricing API access (no AWS credentials in the
    dev sandbox) — the service code/usagetype below are inferred from AWS's
    public EventBridge pricing page structure, not verified against a real
    `get_products` response. Run `bucksawz prices update --services
    EventBridge` and sanity-check `bucksawz prices info` against the
    console's advertised rates before trusting this, and fix the filters
    here if they don't match.

    Service code: AmazonEventBridge. Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [{"Type": "TERM_MATCH", "Field": "regionCode", "Value": region}]
    stored = 0
    for product in _iter_products(pricing, "AmazonEventBridge", filters):
        attrs = product.get("product", {}).get("attributes", {})
        usagetype = attrs.get("usagetype", "")
        if "PutEvents" not in usagetype and "Event-64K-Chunks" not in usagetype:
            continue
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        price_db.upsert("AmazonEventBridge", region, "eventbridge:events", unit, price, desc, db=db)
        stored += 1
    return stored


def fetch_transit_gateway(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    Transit Gateway per-attachment hourly + per-GB data-processed prices for
    `region`. Same hourly-plus-usage shape as NAT Gateway/VPC Interface
    Endpoints.

    Service code: AmazonVPC, product family: Transit Gateway. usagetype
    suffixes are "TransitGateway-Hours" and "TransitGateway-Bytes". Returns
    count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [
        {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Transit Gateway"},
    ]
    stored = 0
    for product in _iter_products(pricing, "AmazonVPC", filters):
        attrs = product.get("product", {}).get("attributes", {})
        usagetype = attrs.get("usagetype", "")
        if usagetype.endswith("TransitGateway-Hours"):
            key = "transitgateway:hourly"
        elif usagetype.endswith("TransitGateway-Bytes"):
            key = "transitgateway:data"
        else:
            continue
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        price_db.upsert("AmazonVPC", region, key, unit, price, desc, db=db)
        stored += 1
    return stored


def fetch_s3files(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    S3 Files cache-storage and cache-request prices for `region`. S3 Files
    (`aws_s3files_file_system`) is a POSIX file system mounted directly onto
    an existing S3 bucket: the underlying object data is billed at the
    bucket's ordinary S3 storage rate (already covered by `fetch_s3` /
    `s3:storage:standard` — not duplicated here), and S3 Files' own
    incremental cost is a per-GB cache-storage rate (only actively-accessed
    data is cached, not the whole bucket) plus per-request GET/PUT charges
    on that cache. Mount targets and access points
    (`aws_s3files_mount_target`, `aws_s3files_access_point`) carry no
    charge at all — see `_price_s3files_mount_target`/`_access_point`.

    NOTE: S3 Files launched after this fetcher's knowledge cutoff and there
    was no live Pricing API access to verify it (no AWS credentials in the
    dev sandbox) — the service code (`AmazonS3Files`), product families, and
    usagetype substrings below are a best-effort guess following this
    codebase's naming conventions for sibling services (AmazonEFS,
    AmazonECR), not confirmed against a real `get_products` response. Run
    `bucksawz prices update --services S3Files` and check `bucksawz prices
    info` against the console's advertised rates (~$0.30/GB-mo cache
    storage was the only number available while writing this) before
    trusting it, and fix the service code/filters here if they don't match.

    Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [{"Type": "TERM_MATCH", "Field": "regionCode", "Value": region}]
    stored = 0
    for product in _iter_products(pricing, "AmazonS3Files", filters):
        attrs = product.get("product", {}).get("attributes", {})
        usagetype = attrs.get("usagetype", "")
        if "Cache-Storage" in usagetype or "CacheStorage" in usagetype:
            key = "s3files:cache"
        elif "Requests-GET" in usagetype or "Get-Requests" in usagetype:
            key = "s3files:requests:get"
        elif "Requests-PUT" in usagetype or "Put-Requests" in usagetype:
            key = "s3files:requests:put"
        else:
            continue
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        price_db.upsert("AmazonS3Files", region, key, unit, price, desc, db=db)
        stored += 1
    return stored


def fetch_opensearch(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    OpenSearch/Elasticsearch Service on-demand data-node instance-hour
    prices, and gp2/gp3 EBS storage rates, for `region`. Dedicated master
    and UltraWarm/cold-storage nodes aren't fetched (see
    `_price_opensearch_domain`'s docstring for the resulting limitation).

    NOTE: written without live Pricing API access (no AWS credentials in
    the dev sandbox) — the product family names below ("Elastic Search
    Instance", "Elastic Search Volume") are inferred from AWS's public
    OpenSearch Service pricing page structure, not verified against a real
    `get_products` response. Run `bucksawz prices update --services
    OpenSearch` and sanity-check `bucksawz prices info` against the
    console's advertised rates before trusting this, and fix the family
    names/filters here if they don't match.

    Service code: AmazonES (unchanged from the Elasticsearch-era service
    code even for OpenSearch domains). Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [{"Type": "TERM_MATCH", "Field": "regionCode", "Value": region}]
    stored = 0
    for product in _iter_products(pricing, "AmazonES", filters):
        attrs = product.get("product", {}).get("attributes", {})
        family = product.get("product", {}).get("productFamily", "")
        if family == "Elastic Search Instance":
            instance_type = attrs.get("instanceType", "")
            if not instance_type:
                continue
            key = f"opensearch:{instance_type}"
        elif family == "Elastic Search Volume":
            volume_type = (attrs.get("volumeType") or "").lower()
            if "general purpose" in volume_type and "gp3" in attrs.get("usagetype", "").lower():
                key = "opensearch:storage:gp3"
            elif "general purpose" in volume_type:
                key = "opensearch:storage:gp2"
            else:
                continue
        else:
            continue
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        price_db.upsert("AmazonES", region, key, unit, price, desc, db=db)
        stored += 1
    return stored


def fetch_redshift(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    Redshift on-demand node-hour prices (product family "Compute Instance")
    and RA3 managed-storage rate (product family "Storage") for `region`.
    Reserved-instance pricing and Redshift Serverless (RPU-hours, a
    separate, unrelated Pricing API product) aren't fetched.
    Service code: AmazonRedshift. Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [{"Type": "TERM_MATCH", "Field": "regionCode", "Value": region}]
    stored = 0
    for product in _iter_products(pricing, "AmazonRedshift", filters):
        attrs = product.get("product", {}).get("attributes", {})
        family = product.get("product", {}).get("productFamily", "")
        if family == "Compute Instance":
            instance_type = attrs.get("instanceType", "")
            if not instance_type:
                continue
            key = f"redshift:{instance_type}"
        elif family == "Storage":
            usagetype = attrs.get("usagetype", "")
            if "ManagedStorage" not in usagetype and "RMS" not in usagetype:
                continue
            key = "redshift:storage"
        else:
            continue
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        price_db.upsert("AmazonRedshift", region, key, unit, price, desc, db=db)
        stored += 1
    return stored


def fetch_backup(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    AWS Backup warm/cold storage and restore per-GB rates for `region`.
    Entirely usage-based like S3/EFS: how much backed-up data a vault holds
    and how much gets restored isn't derivable from `aws_backup_vault`'s or
    `aws_backup_plan`'s own config, so only unit prices are fetched here.

    NOTE: written without live Pricing API access (no AWS credentials in
    the dev sandbox) — the "Backup Storage"/"Storage Snapshot"/"Restore"
    usagetype substrings below are inferred from AWS's public AWS Backup
    pricing page, not verified against a real `get_products` response. Run
    `bucksawz prices update --services Backup` and sanity-check `bucksawz
    prices info` against the console's advertised rates before trusting
    this, and fix the substrings here if they don't match.

    Service code: AWSBackup. Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [{"Type": "TERM_MATCH", "Field": "regionCode", "Value": region}]
    stored = 0
    for product in _iter_products(pricing, "AWSBackup", filters):
        attrs = product.get("product", {}).get("attributes", {})
        usagetype = attrs.get("usagetype", "")
        if "Cold" in usagetype and "Storage" in usagetype:
            key = "backup:storage:cold"
        elif "Warm" in usagetype and "Storage" in usagetype:
            key = "backup:storage:warm"
        elif "Restore" in usagetype:
            key = "backup:restore"
        else:
            continue
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        price_db.upsert("AWSBackup", region, key, unit, price, desc, db=db)
        stored += 1
    return stored


def fetch_msk(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    MSK (Managed Streaming for Kafka) broker instance-hour and EBS
    broker-storage per-GB rates for `region`. MSK Serverless (a separate,
    RPU-hour-and-partition billing model, not `aws_msk_cluster`) isn't
    fetched.

    NOTE: written without live Pricing API access (no AWS credentials in
    the dev sandbox) — the product family names below ("Kafka Broker
    Instance", "Kafka Broker Storage") are inferred from AWS's public MSK
    pricing page structure, not verified against a real `get_products`
    response. Run `bucksawz prices update --services MSK` and sanity-check
    `bucksawz prices info` against the console's advertised rates before
    trusting this, and fix the family names/filters here if they don't
    match.

    Service code: AmazonMSK. Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [{"Type": "TERM_MATCH", "Field": "regionCode", "Value": region}]
    stored = 0
    for product in _iter_products(pricing, "AmazonMSK", filters):
        attrs = product.get("product", {}).get("attributes", {})
        family = product.get("product", {}).get("productFamily", "")
        if family == "Kafka Broker Instance":
            instance_type = attrs.get("instanceType", "")
            if not instance_type:
                continue
            key = f"msk:{instance_type}"
        elif family == "Kafka Broker Storage":
            key = "msk:storage"
        else:
            continue
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        price_db.upsert("AmazonMSK", region, key, unit, price, desc, db=db)
        stored += 1
    return stored


def fetch_eip(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    Public IPv4 address hourly rate for `region`. Since the Feb 1, 2024 AWS
    pricing change, every public IPv4 address costs the same flat
    $0.005/hr whether it's an Elastic IP or not, and whether it's attached
    to a running resource, a stopped one, or nothing at all — the old
    "free while attached, charged while idle" distinction no longer
    applies, so in-use and idle line items are collapsed into a single
    flat key here rather than kept separate.

    NOTE: written without live Pricing API access (no AWS credentials in
    the dev sandbox) — the "PublicIPv4"/"ElasticIP" usagetype substrings
    below are inferred from AWS's public pricing announcement, not
    verified against a real `get_products` response. Run `bucksawz prices
    update --services EIP` and sanity-check `bucksawz prices info` against
    the console's advertised $0.005/hr rate before trusting this, and fix
    the substrings here if they don't match.

    Service code: AmazonVPC, product family: IP Address. Returns count of
    rows stored (this fetcher stores at most 1 — the first matching flat
    rate it finds).
    """
    pricing = _pricing_client(profile)
    filters = [
        {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "IP Address"},
    ]
    for product in _iter_products(pricing, "AmazonVPC", filters):
        attrs = product.get("product", {}).get("attributes", {})
        usagetype = attrs.get("usagetype", "")
        if "PublicIPv4" not in usagetype and "ElasticIP" not in usagetype:
            continue
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        price_db.upsert("AmazonVPC", region, "eip:hourly", unit, price, desc, db=db)
        return 1
    return 0


def fetch_cloudtrail(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    CloudTrail management-event, data-event, and Insights-event per-100K-
    events prices for `region`. Every account gets one free management-
    event trail; charges apply to additional copies of management events,
    all data events, and Insights events. Whether an `aws_cloudtrail`
    resource actually incurs data-event or Insights charges depends on its
    `event_selector`/`advanced_event_selector`/`insight_selector` blocks,
    but the event *volume* behind any of these is never in Terraform
    config — so, like AWS Config, everything here is stored as a unit
    price only (see `_price_cloudtrail`).

    NOTE: written without live Pricing API access (no AWS credentials in
    the dev sandbox) — the "PaidEventsRecorded"/"DataEventsRecorded"/
    "InsightsEventsRecorded" usagetype substrings below are inferred from
    AWS's public CloudTrail pricing page, not verified against a real
    `get_products` response. Run `bucksawz prices update --services
    CloudTrail` and sanity-check `bucksawz prices info` before trusting
    this, and fix the substrings here if they don't match.

    Service code: AWSCloudTrail. Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [{"Type": "TERM_MATCH", "Field": "regionCode", "Value": region}]
    stored = 0
    for product in _iter_products(pricing, "AWSCloudTrail", filters):
        attrs = product.get("product", {}).get("attributes", {})
        usagetype = attrs.get("usagetype", "")
        if "InsightsEventsRecorded" in usagetype:
            key = "cloudtrail:insights"
        elif "DataEventsRecorded" in usagetype:
            key = "cloudtrail:data"
        elif "PaidEventsRecorded" in usagetype:
            key = "cloudtrail:management"
        else:
            continue
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        price_db.upsert("AWSCloudTrail", region, key, unit, price, desc, db=db)
        stored += 1
    return stored


def fetch_guardduty(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    GuardDuty per-GB analysis prices for `region`. Cost is driven entirely
    by the volume of CloudTrail events, VPC Flow Logs, DNS logs, S3 data
    events, EKS audit logs, etc. GuardDuty actually analyzes — none of
    which is derivable from an `aws_guardduty_detector`'s own config, so
    every price here is stored unit-only, same pattern as Config/
    CloudTrail. Only the base CloudTrail/DNS-log analysis tier is fetched;
    the separately-priced VPC Flow Logs, S3 Protection, EKS Protection,
    and Malware Protection tiers (enabled via the newer
    `aws_guardduty_detector_feature` resource, not modeled here) aren't.

    NOTE: written without live Pricing API access (no AWS credentials in
    the dev sandbox) — the usagetype substring below is inferred from
    AWS's public GuardDuty pricing page, not verified against a real
    `get_products` response. Run `bucksawz prices update --services
    GuardDuty` and sanity-check `bucksawz prices info` before trusting
    this.

    Service code: AmazonGuardDuty. Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [{"Type": "TERM_MATCH", "Field": "regionCode", "Value": region}]
    for product in _iter_products(pricing, "AmazonGuardDuty", filters):
        attrs = product.get("product", {}).get("attributes", {})
        usagetype = attrs.get("usagetype", "")
        if "Event" not in usagetype and "CloudTrail" not in usagetype and "Findings" not in usagetype:
            continue
        result = _first_tier_price(product)
        if result is None:
            continue
        unit, price, desc = result
        price_db.upsert("AmazonGuardDuty", region, "guardduty:analysis", unit, price, desc, db=db)
        return 1
    return 0


def fetch_docdb(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    DocumentDB on-demand instance prices for `region`, same shape as RDS.
    Cluster storage and I/O (`aws_docdb_cluster`) are usage-based and not
    fetched here — see `_price_docdb_cluster_instance`'s docstring.

    Service code: AmazonDocDB, product family: Database Instance. Returns
    count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [
        {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Database Instance"},
    ]
    stored = 0
    for product in _iter_products(pricing, "AmazonDocDB", filters):
        attrs = product.get("product", {}).get("attributes", {})
        instance_type = attrs.get("instanceType", "")
        if not instance_type:
            continue
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        key = f"docdb:{instance_type}"
        price_db.upsert("AmazonDocDB", region, key, unit, price, desc, db=db)
        stored += 1
    return stored


def fetch_fsx_windows(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    FSx for Windows File Server SSD/HDD storage and throughput-capacity
    rates for `region`, both fully config-derivable from
    `storage_capacity`/`storage_type`/`throughput_capacity` — see
    `_price_fsx_windows_file_system`.

    NOTE: written without live Pricing API access (no AWS credentials in
    the dev sandbox) — the product family names below ("Storage",
    "Provisioned Throughput") and the SSD/HDD `storageMedia` attribute
    check are inferred from AWS's public FSx pricing page structure, not
    verified against a real `get_products` response. Run `bucksawz prices
    update --services FSxWindows` and sanity-check `bucksawz prices info`
    before trusting this, and fix the filters here if they don't match.

    Service code: AmazonFSx. Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [
        {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
        {"Type": "TERM_MATCH", "Field": "fileSystemType", "Value": "Windows"},
    ]
    stored = 0
    for product in _iter_products(pricing, "AmazonFSx", filters):
        attrs = product.get("product", {}).get("attributes", {})
        family = product.get("product", {}).get("productFamily", "")
        media = (attrs.get("storageMedia") or "").upper()
        if family == "Storage":
            if "SSD" in media:
                key = "fsx:windows:storage:ssd"
            elif "HDD" in media:
                key = "fsx:windows:storage:hdd"
            else:
                continue
        elif family == "Provisioned Throughput":
            key = "fsx:windows:throughput"
        else:
            continue
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        price_db.upsert("AmazonFSx", region, key, unit, price, desc, db=db)
        stored += 1
    return stored


def fetch_acmpca(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    ACM Private Certificate Authority monthly per-CA fee, by usage mode
    (general-purpose vs. short-lived), plus the first-tier
    certificate-issuance rate for `region`. Public ACM certificates
    (`aws_acm_certificate` without a `certificate_authority_arn`) are free
    and have no pricer at all — only the private-CA resource carries cost.

    NOTE: written without live Pricing API access (no AWS credentials in
    the dev sandbox) — the service code (`AWSCertificateManager`), the
    "PrivateCertificateAuthority"/"CertificatesIssued" usagetype
    substrings, and the general-purpose/short-lived distinction below are
    inferred from AWS's public ACM Private CA pricing page, not verified
    against a real `get_products` response. Run `bucksawz prices update
    --services ACMPCA` and sanity-check `bucksawz prices info` against the
    console's advertised $400/mo (general-purpose) and $50/mo
    (short-lived) rates before trusting this, and fix the substrings here
    if they don't match.

    Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [{"Type": "TERM_MATCH", "Field": "regionCode", "Value": region}]
    stored = 0
    for product in _iter_products(pricing, "AWSCertificateManager", filters):
        attrs = product.get("product", {}).get("attributes", {})
        usagetype = attrs.get("usagetype", "")
        if "ShortLived" in usagetype and "PrivateCertificateAuthority" in usagetype:
            key = "acmpca:monthly:short_lived"
        elif "PrivateCertificateAuthority" in usagetype:
            key = "acmpca:monthly:general_purpose"
        elif "CertificatesIssued" in usagetype:
            result = _first_tier_price(product)
            if result is None:
                continue
            unit, price, desc = result
            price_db.upsert("AWSCertificateManager", region, "acmpca:certificate", unit, price, desc, db=db)
            stored += 1
            continue
        else:
            continue
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        price_db.upsert("AWSCertificateManager", region, key, unit, price, desc, db=db)
        stored += 1
    return stored


def fetch_athena(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    Athena per-TB-scanned query price for `region`. Fully usage-based:
    how much data a query scans isn't in `aws_athena_workgroup`'s own
    config — `bytes_scanned_cutoff_per_query` caps a single query's cost
    but doesn't set it, and most workgroups don't even set a cutoff. See
    `_price_athena_workgroup`.

    Service code: AmazonAthena, product family: Amazon Athena. Returns
    count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [
        {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Amazon Athena"},
    ]
    for product in _iter_products(pricing, "AmazonAthena", filters):
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        price_db.upsert("AmazonAthena", region, "athena:scanned", unit, price, desc, db=db)
        return 1
    return 0


def fetch_fsx_lustre(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    FSx for Lustre per-GB-month storage rates for `region`, keyed by
    `deployment_type` + `storage_type`. Unlike FSx for Windows, Lustre has
    no independently priced throughput_capacity knob — throughput scales
    with `per_unit_storage_throughput` (MB/s per TiB) for PERSISTENT
    deployments, which shifts the per-GB storage rate itself rather than
    adding a separate line item; that per-tier variation isn't captured
    here, only a single representative rate per deployment/storage-type
    combination — see `_price_fsx_lustre_file_system`'s docstring.

    NOTE: written without live Pricing API access (no AWS credentials in
    the dev sandbox) — the `deploymentOption`/`storageMedia` attribute
    names below are inferred from AWS's public FSx pricing page
    structure, not verified against a real `get_products` response. Run
    `bucksawz prices update --services FSxLustre` and sanity-check
    `bucksawz prices info` before trusting this.

    Service code: AmazonFSx. Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [
        {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
        {"Type": "TERM_MATCH", "Field": "fileSystemType", "Value": "Lustre"},
        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Storage"},
    ]
    stored = 0
    for product in _iter_products(pricing, "AmazonFSx", filters):
        attrs = product.get("product", {}).get("attributes", {})
        deployment = (attrs.get("deploymentOption") or "").upper().replace(" ", "_")
        media = (attrs.get("storageMedia") or "SSD").upper()
        if not deployment:
            continue
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        key = f"fsx:lustre:{deployment}:{media}"
        price_db.upsert("AmazonFSx", region, key, unit, price, desc, db=db)
        stored += 1
    return stored


def fetch_neptune(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    Neptune on-demand instance prices for `region`, same shape as RDS/
    DocumentDB. Cluster storage and I/O (`aws_neptune_cluster`) are
    usage-based and not fetched here.

    Service code: AmazonNeptune, product family: Database Instance.
    Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [
        {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Database Instance"},
    ]
    stored = 0
    for product in _iter_products(pricing, "AmazonNeptune", filters):
        attrs = product.get("product", {}).get("attributes", {})
        instance_type = attrs.get("instanceType", "")
        if not instance_type:
            continue
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        key = f"neptune:{instance_type}"
        price_db.upsert("AmazonNeptune", region, key, unit, price, desc, db=db)
        stored += 1
    return stored


def fetch_global_accelerator(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    Global Accelerator's flat fixed hourly fee and per-GB data-transfer
    premium. The fixed fee is *global*, not per-region (Global Accelerator
    is a global service, same as Route 53/CloudFront) — stored under
    whatever region key `prices update` requests, same handling as
    `fetch_route53`. The data-transfer premium does vary somewhat by the
    traffic's ingress/egress region pair, which a single Terraform
    resource can't express, so only a flat representative rate is stored,
    same simplification as WAF's capacity-unit tiers.

    NOTE: written without live Pricing API access (no AWS credentials in
    the dev sandbox) — the usagetype substrings below are inferred from
    AWS's public Global Accelerator pricing page, not verified against a
    real `get_products` response. Run `bucksawz prices update --services
    GlobalAccelerator` and sanity-check `bucksawz prices info` against the
    console's advertised $0.025/hr fixed fee before trusting this.

    Service code: AWSGlobalAccelerator. Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters: list[dict] = []
    stored = 0
    for product in _iter_products(pricing, "AWSGlobalAccelerator", filters):
        attrs = product.get("product", {}).get("attributes", {})
        usagetype = attrs.get("usagetype", "")
        if "FixedFee" in usagetype or "Hourly" in usagetype:
            key = "globalaccelerator:hourly"
        elif "DataTransfer" in usagetype or "Premium" in usagetype:
            key = "globalaccelerator:data"
        else:
            continue
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        price_db.upsert("AWSGlobalAccelerator", region, key, unit, price, desc, db=db)
        stored += 1
    return stored


def fetch_mq(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    Amazon MQ on-demand broker-instance-hour and EBS storage per-GB-month
    prices for `region`, keyed by instance type and engine (ActiveMQ vs.
    RabbitMQ — RabbitMQ has no per-GB storage charge on `mq.t3.micro`/`mq.m5`
    EBS-backed brokers the way ActiveMQ does, but that distinction isn't
    modeled here; see `_price_mq_broker`'s docstring).

    NOTE: written without live Pricing API access (no AWS credentials in
    the dev sandbox) — the service code (`AmazonMQ`) and usagetype
    substrings below are inferred from AWS's public Amazon MQ pricing
    page, not verified against a real `get_products` response. Run
    `bucksawz prices update --services MQ` and sanity-check `bucksawz
    prices info` before trusting this.

    Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [{"Type": "TERM_MATCH", "Field": "regionCode", "Value": region}]
    stored = 0
    for product in _iter_products(pricing, "AmazonMQ", filters):
        attrs = product.get("product", {}).get("attributes", {})
        family = product.get("product", {}).get("productFamily", "")
        if family == "Broker Instances":
            instance_type = attrs.get("instanceType", "")
            if not instance_type:
                continue
            key = f"mq:{instance_type}"
        elif family == "Storage":
            key = "mq:storage"
        else:
            continue
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        price_db.upsert("AmazonMQ", region, key, unit, price, desc, db=db)
        stored += 1
    return stored


def fetch_vpn(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    Site-to-Site VPN connection-hour rate (flat, same for every connection
    regardless of the customer/transit gateway it attaches to) plus
    AWS Client VPN's endpoint-association-hour and connection-hour rates,
    for `region`.

    NOTE: written without live Pricing API access (no AWS credentials in
    the dev sandbox) — the productFamily/usagetype substrings below are
    inferred from AWS's public VPN pricing page, not verified against a
    real `get_products` response. Run `bucksawz prices update --services
    VPN` and sanity-check `bucksawz prices info` before trusting this.

    Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [{"Type": "TERM_MATCH", "Field": "regionCode", "Value": region}]
    stored = 0
    for product in _iter_products(pricing, "AmazonVPC", filters):
        attrs = product.get("product", {}).get("attributes", {})
        family = product.get("product", {}).get("productFamily", "")
        usagetype = attrs.get("usagetype", "")
        if family == "Cloud Connectivity" and "VPN-Usage" in usagetype:
            key = "vpn:sitetosite:hourly"
        elif "ClientVPN-EndpointHours" in usagetype:
            key = "vpn:clientvpn:association:hourly"
        elif "ClientVPN-ConnectionHours" in usagetype:
            key = "vpn:clientvpn:connection:hourly"
        else:
            continue
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        price_db.upsert("AmazonVPC", region, key, unit, price, desc, db=db)
        stored += 1
    return stored


def fetch_direct_connect(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    Direct Connect dedicated/hosted port-hour rate for `region`, keyed by
    port speed (e.g. "1Gbps", "10Gbps"). Data transfer out over the
    connection is usage-based and isn't fetched here.

    NOTE: written without live Pricing API access (no AWS credentials in
    the dev sandbox) — the attribute names below (`portSpeed`) are
    inferred from AWS's public Direct Connect pricing page, not verified
    against a real `get_products` response. Run `bucksawz prices update
    --services DirectConnect` and sanity-check `bucksawz prices info`
    before trusting this.

    Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [
        {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Direct Connect Port"},
    ]
    stored = 0
    for product in _iter_products(pricing, "AWSDirectConnect", filters):
        attrs = product.get("product", {}).get("attributes", {})
        port_speed = attrs.get("portSpeed", "")
        if not port_speed:
            continue
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        key = f"directconnect:port:{port_speed.replace(' ', '').lower()}"
        price_db.upsert("AWSDirectConnect", region, key, unit, price, desc, db=db)
        stored += 1
    return stored


def fetch_appsync(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    AppSync per-request (query/mutation) and real-time-subscription
    connection-minute rates for `region`. Fully usage-based — request and
    connection volume aren't derivable from the API's own config.

    NOTE: written without live Pricing API access (no AWS credentials in
    the dev sandbox) — the usagetype substrings below are inferred from
    AWS's public AppSync pricing page, not verified against a real
    `get_products` response. Run `bucksawz prices update --services
    AppSync` and sanity-check `bucksawz prices info` before trusting this.

    Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [{"Type": "TERM_MATCH", "Field": "regionCode", "Value": region}]
    stored = 0
    for product in _iter_products(pricing, "AWSAppSync", filters):
        attrs = product.get("product", {}).get("attributes", {})
        usagetype = attrs.get("usagetype", "")
        if "RequestOps" in usagetype or "Request-Ops" in usagetype:
            key = "appsync:requests"
        elif "ConnMins" in usagetype or "Connection-Mins" in usagetype:
            key = "appsync:connectionminutes"
        elif "MsgOps" in usagetype or "Message-Ops" in usagetype:
            key = "appsync:messages"
        else:
            continue
        result = _first_tier_price(product)
        if result is None:
            continue
        unit, price, desc = result
        price_db.upsert("AWSAppSync", region, key, unit, price, desc, db=db)
        stored += 1
    return stored


def fetch_cognito(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    Cognito User Pools monthly-active-user (MAU) rate for `region` — the
    first (cheapest/free) pricing tier is stored as a flat representative
    rate, same simplification as `fetch_waf`'s WACU pricing: real MAU
    pricing is tiered and further split by whether advanced security
    features are enabled, neither of which is resolvable from a single
    Terraform plan.

    NOTE: written without live Pricing API access (no AWS credentials in
    the dev sandbox) — the usagetype substring below is inferred from
    AWS's public Cognito pricing page, not verified against a real
    `get_products` response. Run `bucksawz prices update --services
    Cognito` and sanity-check `bucksawz prices info` before trusting this.

    Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [{"Type": "TERM_MATCH", "Field": "regionCode", "Value": region}]
    stored = 0
    for product in _iter_products(pricing, "AmazonCognitoSync", filters):
        attrs = product.get("product", {}).get("attributes", {})
        usagetype = attrs.get("usagetype", "")
        if "MAU" not in usagetype:
            continue
        result = _first_tier_price(product)
        if result is None:
            continue
        unit, price, desc = result
        price_db.upsert("AmazonCognitoSync", region, "cognito:mau", unit, price, desc, db=db)
        stored += 1
    return stored


def fetch_glue(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    AWS Glue DPU-hour rate for `region`, shared by jobs and crawlers —
    both are billed the same per-DPU-hour rate. Fully usage-based: run
    frequency and duration aren't derivable from a job/crawler's own
    config.

    NOTE: written without live Pricing API access (no AWS credentials in
    the dev sandbox) — the usagetype substring below is inferred from
    AWS's public Glue pricing page, not verified against a real
    `get_products` response. Run `bucksawz prices update --services Glue`
    and sanity-check `bucksawz prices info` before trusting this.

    Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [{"Type": "TERM_MATCH", "Field": "regionCode", "Value": region}]
    stored = 0
    for product in _iter_products(pricing, "AWSGlue", filters):
        attrs = product.get("product", {}).get("attributes", {})
        usagetype = attrs.get("usagetype", "")
        if "DPU-Hour" not in usagetype:
            continue
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        price_db.upsert("AWSGlue", region, "glue:dpuhour", unit, price, desc, db=db)
        stored += 1
    return stored


def fetch_sagemaker(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    SageMaker on-demand ML instance-hour prices for `region`, keyed by
    instance type. The same rate is used for notebook instances and
    real-time endpoint hosting instances (AWS prices these the same per
    instance type; training/batch-transform/processing instance-hours
    aren't fetched here since there's no long-lived Terraform resource
    representing them).

    NOTE: written without live Pricing API access (no AWS credentials in
    the dev sandbox) — the productFamily below is inferred from AWS's
    public SageMaker pricing page, not verified against a real
    `get_products` response. Run `bucksawz prices update --services
    SageMaker` and sanity-check `bucksawz prices info` before trusting
    this.

    Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [
        {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "ML Instance"},
    ]
    stored = 0
    for product in _iter_products(pricing, "AmazonSageMaker", filters):
        attrs = product.get("product", {}).get("attributes", {})
        instance_type = attrs.get("instanceType", "")
        if not instance_type:
            continue
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        key = f"sagemaker:{instance_type}"
        price_db.upsert("AmazonSageMaker", region, key, unit, price, desc, db=db)
        stored += 1
    return stored


def fetch_cloudhsm(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    CloudHSM HSM-hour rate for `region` — flat, no instance-type variation
    (there's only one HSM hardware type). Keyed per HSM
    (`aws_cloudhsm_v2_hsm`); the cluster resource itself carries no
    charge of its own.

    NOTE: written without live Pricing API access (no AWS credentials in
    the dev sandbox) — the productFamily below is inferred from AWS's
    public CloudHSM pricing page, not verified against a real
    `get_products` response. Run `bucksawz prices update --services
    CloudHSM` and sanity-check `bucksawz prices info` before trusting
    this.

    Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [{"Type": "TERM_MATCH", "Field": "regionCode", "Value": region}]
    stored = 0
    for product in _iter_products(pricing, "AWSCloudHSM", filters):
        result = _ondemand_price(product)
        if result is None:
            continue
        unit, price, desc = result
        price_db.upsert("AWSCloudHSM", region, "cloudhsm:hourly", unit, price, desc, db=db)
        stored += 1
    return stored


def fetch_macie(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    Macie per-GB data-evaluation rate for `region`, shared by the account-
    level enablement resource and individual classification jobs. A
    single representative (first-tier) rate is stored — real pricing is
    tiered by cumulative GB processed per month, which isn't resolvable
    from a single Terraform plan.

    NOTE: written without live Pricing API access (no AWS credentials in
    the dev sandbox) — the usagetype substring below is inferred from
    AWS's public Macie pricing page, not verified against a real
    `get_products` response. Run `bucksawz prices update --services
    Macie` and sanity-check `bucksawz prices info` before trusting this.

    Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [{"Type": "TERM_MATCH", "Field": "regionCode", "Value": region}]
    stored = 0
    for product in _iter_products(pricing, "AmazonMacie", filters):
        attrs = product.get("product", {}).get("attributes", {})
        usagetype = attrs.get("usagetype", "")
        if "DataInspected" not in usagetype and "GB" not in usagetype:
            continue
        result = _first_tier_price(product)
        if result is None:
            continue
        unit, price, desc = result
        price_db.upsert("AmazonMacie", region, "macie:gb", unit, price, desc, db=db)
        stored += 1
    return stored


def fetch_inspector(
    region: str, profile: Optional[str] = None, db: Optional[Path] = None
) -> int:
    """
    Inspector V2 continuous-scanning rates for `region`: EC2
    instance-month, ECR image scan, and Lambda function-month. Fully
    usage-based downstream — actual instance/image/function counts are
    account-wide and aren't tied to the enabler resource's own config.

    NOTE: written without live Pricing API access (no AWS credentials in
    the dev sandbox) — the productFamily/usagetype substrings below are
    inferred from AWS's public Inspector pricing page, not verified
    against a real `get_products` response. Run `bucksawz prices update
    --services Inspector` and sanity-check `bucksawz prices info` before
    trusting this.

    Returns count of rows stored.
    """
    pricing = _pricing_client(profile)
    filters = [{"Type": "TERM_MATCH", "Field": "regionCode", "Value": region}]
    stored = 0
    for product in _iter_products(pricing, "AmazonInspectorV2", filters):
        attrs = product.get("product", {}).get("attributes", {})
        usagetype = attrs.get("usagetype", "")
        if "EC2" in usagetype:
            key = "inspector:ec2"
        elif "ECR" in usagetype:
            key = "inspector:ecr"
        elif "Lambda" in usagetype:
            key = "inspector:lambda"
        else:
            continue
        result = _first_tier_price(product)
        if result is None:
            continue
        unit, price, desc = result
        price_db.upsert("AmazonInspectorV2", region, key, unit, price, desc, db=db)
        stored += 1
    return stored


_FETCHERS: dict[str, object] = {
    "ECS": fetch_fargate,
    "Lambda": fetch_lambda,
    "EC2": fetch_ec2_instances,
    "EBS": fetch_ebs,
    "RDS": fetch_rds_instances,
    "ElastiCache": fetch_elasticache,
    "S3": fetch_s3,
    "SQS": fetch_sqs,
    "CloudWatch": fetch_cloudwatch,
    "ELB": fetch_elb,
    "SecretsManager": fetch_secretsmanager,
    "Route53": fetch_route53,
    "KMS": fetch_kms,
    "WAF": fetch_waf,
    "DataTransfer": fetch_data_transfer,
    "NATGateway": fetch_nat_gateway,
    "Config": fetch_config,
    "EKS": fetch_eks,
    "DynamoDB": fetch_dynamodb,
    "VPCEndpoint": fetch_vpc_endpoint,
    "SNS": fetch_sns,
    "EFS": fetch_efs,
    "ECR": fetch_ecr,
    "APIGateway": fetch_apigateway,
    "CloudFront": fetch_cloudfront,
    "Kinesis": fetch_kinesis,
    "StepFunctions": fetch_stepfunctions,
    "EventBridge": fetch_eventbridge,
    "TransitGateway": fetch_transit_gateway,
    "S3Files": fetch_s3files,
    "OpenSearch": fetch_opensearch,
    "Redshift": fetch_redshift,
    "Backup": fetch_backup,
    "MSK": fetch_msk,
    "EIP": fetch_eip,
    "CloudTrail": fetch_cloudtrail,
    "GuardDuty": fetch_guardduty,
    "DocDB": fetch_docdb,
    "FSxWindows": fetch_fsx_windows,
    "ACMPCA": fetch_acmpca,
    "Athena": fetch_athena,
    "FSxLustre": fetch_fsx_lustre,
    "Neptune": fetch_neptune,
    "GlobalAccelerator": fetch_global_accelerator,
    "MQ": fetch_mq,
    "VPN": fetch_vpn,
    "DirectConnect": fetch_direct_connect,
    "AppSync": fetch_appsync,
    "Cognito": fetch_cognito,
    "Glue": fetch_glue,
    "SageMaker": fetch_sagemaker,
    "CloudHSM": fetch_cloudhsm,
    "Macie": fetch_macie,
    "Inspector": fetch_inspector,
}

ALL_SERVICES: list[str] = list(_FETCHERS.keys())


def fetch_all(
    regions: list[str],
    services: Optional[list[str]] = None,
    profile: Optional[str] = None,
    db: Optional[Path] = None,
) -> dict[str, int]:
    """
    Fetch prices for the given services and regions.
    Returns {service: total_rows_stored}.
    """
    if services is None:
        services = ALL_SERVICES
    totals: dict[str, int] = {}
    for svc in services:
        fn = _FETCHERS.get(svc)
        if fn is None:
            print(f"  [skip] unknown service '{svc}' — valid: {', '.join(ALL_SERVICES)}")
            continue
        svc_total = 0
        for region in regions:
            print(f"  [{svc}] {region}…", flush=True)
            try:
                n = fn(region, profile=profile, db=db)  # type: ignore[call-arg]
                svc_total += n
                print(f"  [{svc}] {region}: {n} prices stored")
            except Exception as exc:
                print(f"  [{svc}] {region}: error — {exc}")
        totals[svc] = svc_total
    return totals
