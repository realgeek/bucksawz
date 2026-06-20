"""
AWS Cost Explorer enrichment.
Pulls GetCostAndUsage for the lookback window and merges actuals into
the infracost output, filling in usage-based cost estimates.

Results are cached locally for 7 days (configurable via --cache-ttl).
Cache lives in ~/.cache/bucksawz/. Override with $BUCKSAWZ_CACHE_DIR.
"""
from __future__ import annotations
import json
from datetime import date, timedelta
from typing import Optional
import boto3
from ..schema.infracost import InfracostOutput, Resource
from .cache import get as cache_get, put as cache_put, cache_key

_DEFAULT_TTL_DAYS = 7


def _ce_client(profile: Optional[str], region: str):
    session = boto3.Session(profile_name=profile, region_name=region)
    return session.client("ce")


def _date_range(lookback_days: int) -> tuple[str, str]:
    end = date.today()
    start = end - timedelta(days=lookback_days)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _get_actuals_by_service(
    ce,
    start: str,
    end: str,
    profile: Optional[str],
    region: str,
    ttl_days: int,
) -> dict[str, float]:
    """Total spend by AWS service for the lookback period, cached."""
    key = cache_key("ce_actuals_by_service", profile or "default", region, start, end)
    cached = cache_get(key, ttl_days=ttl_days)
    if cached is not None:
        print(f"  [cache hit] actuals_by_service ({start}→{end})")
        return cached

    print(f"  [aws] fetching Cost Explorer actuals ({start}→{end})…")
    resp = ce.get_cost_and_usage(
        TimePeriod={"Start": start, "End": end},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )
    totals: dict[str, float] = {}
    for result in resp.get("ResultsByTime", []):
        for group in result.get("Groups", []):
            service = group["Keys"][0]
            amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
            totals[service] = totals.get(service, 0.0) + amount

    cache_put(key, totals)
    return totals


def _get_forecast(
    ce,
    profile: Optional[str],
    region: str,
    ttl_days: int,
) -> Optional[float]:
    """30-day forward cost forecast from today, cached."""
    today = date.today()
    forecast_end = today + timedelta(days=30)
    start_str = today.strftime("%Y-%m-%d")
    end_str = forecast_end.strftime("%Y-%m-%d")

    key = cache_key("ce_forecast", profile or "default", region, start_str, end_str)
    cached = cache_get(key, ttl_days=ttl_days)
    if cached is not None:
        print(f"  [cache hit] forecast")
        return cached

    print(f"  [aws] fetching cost forecast…")
    try:
        resp = ce.get_cost_forecast(
            TimePeriod={"Start": start_str, "End": end_str},
            Metric="UNBLENDED_COST",
            Granularity="MONTHLY",
        )
        result = float(resp["Total"]["Amount"])
        cache_put(key, result)
        return result
    except Exception:
        return None


def enrich_output(
    output: InfracostOutput,
    lookback_days: int = 90,
    profile: Optional[str] = None,
    region: str = "us-east-1",
    cache_ttl_days: int = _DEFAULT_TTL_DAYS,
    force_refresh: bool = False,
    cloudwatch: bool = True,
) -> dict:
    """
    Returns a dict (JSON-serialisable) that extends the infracost output
    with a top-level 'historical' key containing Cost Explorer actuals.

    Results are cached for cache_ttl_days (default 7). Pass force_refresh=True
    to bypass the cache and re-fetch from AWS.
    """
    if force_refresh:
        # Invalidate relevant cache entries before fetching
        from .cache import invalidate
        start, end = _date_range(lookback_days)
        invalidate(cache_key("ce_actuals_by_service", profile or "default", region, start, end))
        today = date.today()
        invalidate(cache_key("ce_forecast", profile or "default", region,
                             today.strftime("%Y-%m-%d"),
                             (today + timedelta(days=30)).strftime("%Y-%m-%d")))

    ce = _ce_client(profile, region)
    start, end = _date_range(lookback_days)

    actuals_by_service = _get_actuals_by_service(
        ce, start, end, profile, region, cache_ttl_days
    )
    forecast = _get_forecast(ce, profile, region, cache_ttl_days)

    months = max(lookback_days / 30, 1)
    monthly_actuals = {k: v / months for k, v in actuals_by_service.items()}

    result = {
        "version": output.version,
        "currency": output.currency,
        "timeGenerated": output.time_generated,
        "totalMonthlyCost": output.total_monthly_cost,
        "historical": {
            "lookbackDays": lookback_days,
            "start": start,
            "end": end,
            "actualsByService": actuals_by_service,
            "monthlyAverageByService": monthly_actuals,
            "forecastNextMonth": forecast,
            "cacheTtlDays": cache_ttl_days,
        },
        "projects": [],
    }

    # CloudWatch usage-based enrichment
    cw_actuals: dict[str, dict] = {}
    if cloudwatch:
        from .cloudwatch import enrich_with_cloudwatch
        all_resources = [
            r
            for p in output.projects if p.breakdown
            for r in p.breakdown.resources
        ]
        print(f"  [aws] fetching CloudWatch metrics for usage-based resources…")
        cw_actuals = enrich_with_cloudwatch(
            resources=all_resources,
            profile=profile,
            region=region,
            lookback_days=lookback_days,
            ttl_days=cache_ttl_days,
        )
        if cw_actuals:
            print(f"  [cloudwatch] enriched {len(cw_actuals)} resources")

    for p in output.projects:
        proj_dict = {
            "name": p.name,
            "metadata": p.metadata,
            "monthlyCost": p.monthly_cost(),
            "resources": [],
        }
        if p.breakdown:
            for r in p.breakdown.resources:
                proj_dict["resources"].append(
                    _enrich_resource(r, monthly_actuals, cw_actuals)
                )
        result["projects"].append(proj_dict)

    return result


_SVC_MAP = {
    "EC2": "Amazon Elastic Compute Cloud - Compute",
    "ELB": "Amazon Elastic Load Balancing",
    "RDS": "Amazon Relational Database Service",
    "S3": "Amazon Simple Storage Service",
    "Lambda": "AWS Lambda",
    "CloudFront": "Amazon CloudFront",
    "Route 53": "Amazon Route 53",
    "SQS": "Amazon Simple Queue Service",
    "SNS": "Amazon Simple Notification Service",
    "ElastiCache": "Amazon ElastiCache",
    "CloudWatch": "Amazon CloudWatch",
    "NAT Gateway": "Amazon Virtual Private Cloud",
    "EBS": "Amazon Elastic Block Store",
    "Secrets/SSM": "AWS Secrets Manager",
    "API Gateway": "Amazon API Gateway",
    "ECS/ECR": "Amazon Elastic Container Service",
}


def _enrich_resource(
    resource: Resource,
    actuals_by_service: dict[str, float],
    cw_actuals: dict[str, dict] | None = None,
) -> dict:
    svc = resource.aws_service()
    aws_svc_name = _SVC_MAP.get(svc)
    actual_monthly = actuals_by_service.get(aws_svc_name) if aws_svc_name else None
    cw = (cw_actuals or {}).get(resource.name, {})

    return {
        "name": resource.name,
        "resourceType": resource.resource_type,
        "tags": resource.tags,
        "monthlyCost": resource.total_monthly_cost(),
        "awsService": svc,
        "historical": {
            "actualMonthlyServiceTotal": actual_monthly,
            "cloudwatchActuals": cw or None,
        },
    }
