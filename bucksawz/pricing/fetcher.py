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
        if "request" in group:
            key = "lambda:requests"
        elif "duration" in group:
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


_FETCHERS: dict[str, object] = {
    "ECS": fetch_fargate,
    "Lambda": fetch_lambda,
    "EC2": fetch_ec2_instances,
    "RDS": fetch_rds_instances,
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
