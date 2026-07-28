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
