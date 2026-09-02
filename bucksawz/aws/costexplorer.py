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


def _organizations_client(profile: Optional[str]):
    # AWS Organizations is a global service reachable only from us-east-1,
    # regardless of the --aws-region the rest of enrich uses.
    session = boto3.Session(profile_name=profile, region_name="us-east-1")
    return session.client("organizations")


def _date_range(lookback_days: int) -> tuple[str, str]:
    end = date.today()
    start = end - timedelta(days=lookback_days)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _get_actuals_by_account_service(
    ce,
    start: str,
    end: str,
    profile: Optional[str],
    region: str,
    ttl_days: int,
) -> dict[str, dict[str, float]]:
    """
    Total spend grouped by (LINKED_ACCOUNT, SERVICE) for the lookback period.
    Returns {account_id: {service_name: total_cost}}.

    When called from a management/payer account this includes all member accounts.
    Single-account callers get a one-entry dict keyed by their own account ID.
    Results are cached for ttl_days.
    """
    key = cache_key("ce_actuals_by_account_service", profile or "default", region, start, end)
    cached = cache_get(key, ttl_days=ttl_days)
    if cached is not None:
        print(f"  [cache hit] actuals_by_account_service ({start}→{end})")
        return cached

    print(f"  [aws] fetching Cost Explorer actuals by account+service ({start}→{end})…")
    by_account: dict[str, dict[str, float]] = {}
    paginator = ce.get_paginator("get_cost_and_usage")
    for page in paginator.paginate(
        TimePeriod={"Start": start, "End": end},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        GroupBy=[
            {"Type": "DIMENSION", "Key": "LINKED_ACCOUNT"},
            {"Type": "DIMENSION", "Key": "SERVICE"},
        ],
    ):
        for period in page.get("ResultsByTime", []):
            for group in period.get("Groups", []):
                account_id, service = group["Keys"]
                amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
                by_account.setdefault(account_id, {})
                by_account[account_id][service] = (
                    by_account[account_id].get(service, 0.0) + amount
                )

    cache_put(key, by_account)
    return by_account


def _aggregate_by_service(by_account: dict[str, dict[str, float]]) -> dict[str, float]:
    """Collapse the account×service matrix to a single by-service total."""
    totals: dict[str, float] = {}
    for svc_map in by_account.values():
        for svc, cost in svc_map.items():
            totals[svc] = totals.get(svc, 0.0) + cost
    return totals


def _account_totals(by_account: dict[str, dict[str, float]]) -> dict[str, float]:
    """Collapse the account×service matrix to a total per account."""
    return {acct: sum(svcs.values()) for acct, svcs in by_account.items()}


def _account_alias_map(
    profile: Optional[str], cache_ttl_days: int = _DEFAULT_TTL_DAYS
) -> dict[str, str]:
    """
    {account_id: account_name} via AWS Organizations `list_accounts`.

    Only the management account (or a delegated administrator) can call this;
    a member-account profile gets AccessDeniedException / AWSOrganizationsNotInUseException,
    which is expected and not an error worth surfacing — just means no aliases,
    same as a single-account (non-Organizations) setup. Account names change
    rarely, so this reuses the same cache TTL as the rest of enrich rather
    than needing its own knob.
    """
    key = cache_key("org_account_aliases", profile or "default")
    cached = cache_get(key, ttl_days=cache_ttl_days)
    if cached is not None:
        return cached

    aliases: dict[str, str] = {}
    try:
        org = _organizations_client(profile)
        paginator = org.get_paginator("list_accounts")
        for page in paginator.paginate():
            for acct in page.get("Accounts", []):
                account_id = acct.get("Id")
                name = acct.get("Name")
                if account_id and name:
                    aliases[account_id] = name
    except Exception:
        return {}

    cache_put(key, aliases)
    return aliases


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


def fetch_data_transfer_actuals(
    lookback_days: int,
    profile: Optional[str],
    region: str,
    cache_ttl_days: int = _DEFAULT_TTL_DAYS,
) -> Optional[dict[str, float]]:
    """
    Real monthly internet-egress and inter-AZ data-transfer volume from Cost
    Explorer, as an alternative to `--usage-file` when `price-state` has AWS
    account access — see pricer.estimate_data_transfer_cost, which accepts
    either source through the same {internet_egress_gb_month, inter_az_gb_month}
    shape used by usage_file.data_transfer_usage.

    Filters to SERVICE "EC2 - Other" (where AWS buckets data-transfer line
    items in Cost Explorer) and the target REGION, grouped by USAGE_TYPE,
    Metric UsageQuantity, averaged over the lookback window. Classifies by
    usage-type suffix the same way fetch_data_transfer (fetcher.py)
    classifies the Pricing API's `transferType`: "-Out-Bytes" (excluding
    "In-Bytes") is internet egress, "-Regional-Bytes" is inter-AZ.

    NOTE: written without live Cost Explorer access (no AWS credentials in
    the dev sandbox) — the usage-type suffixes are inferred from published
    CUR column documentation, not verified against a real GetCostAndUsage
    response. Validate against `aws ce get-cost-and-usage` output for a real
    account with data-transfer spend before trusting the numbers, and adjust
    the suffixes here if they don't match.

    Returns None if Cost Explorer has no matching usage in the lookback
    window (e.g. no data-transfer spend), so callers can fall back to
    `--usage-file`.
    """
    ce = _ce_client(profile, region)
    start, end = _date_range(lookback_days)

    key = cache_key("ce_data_transfer_usage", profile or "default", region, start, end)
    cached = cache_get(key, ttl_days=cache_ttl_days)
    if cached is not None:
        print(f"  [cache hit] data_transfer_usage ({start}→{end})")
        return cached

    print(f"  [aws] fetching Cost Explorer data-transfer usage ({start}→{end})…")
    months = max(lookback_days / 30, 1)
    egress_gb = 0.0
    inter_az_gb = 0.0
    found = False

    paginator = ce.get_paginator("get_cost_and_usage")
    for page in paginator.paginate(
        TimePeriod={"Start": start, "End": end},
        Granularity="MONTHLY",
        Metrics=["UsageQuantity"],
        Filter={
            "And": [
                {"Dimensions": {"Key": "SERVICE", "Values": ["EC2 - Other"]}},
                {"Dimensions": {"Key": "REGION", "Values": [region]}},
            ]
        },
        GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
    ):
        for period in page.get("ResultsByTime", []):
            for group in period.get("Groups", []):
                [usage_type] = group["Keys"]
                qty = float(group["Metrics"]["UsageQuantity"]["Amount"])
                if usage_type.endswith("-Out-Bytes") and "In-Bytes" not in usage_type:
                    egress_gb += qty
                    found = True
                elif usage_type.endswith("-Regional-Bytes"):
                    inter_az_gb += qty
                    found = True

    if not found:
        return None

    result = {
        "internet_egress_gb_month": egress_gb / months,
        "inter_az_gb_month": inter_az_gb / months,
    }
    cache_put(key, result)
    return result


def enrich_output(
    output: InfracostOutput,
    lookback_days: int = 90,
    profile: Optional[str] = None,
    region: str = "us-east-1",
    cache_ttl_days: int = _DEFAULT_TTL_DAYS,
    force_refresh: bool = False,
    cloudwatch: bool = True,
    cloudwatch_regions: Optional[list[str]] = None,
) -> dict:
    """
    Returns a dict (JSON-serialisable) that extends the infracost output
    with a top-level 'historical' key containing Cost Explorer actuals.

    Results are cached for cache_ttl_days (default 7). Pass force_refresh=True
    to bypass the cache and re-fetch from AWS.

    `region` is a single region used for the CE/Organizations API clients and
    cache keys — Cost Explorer's cost-by-account/service totals are already
    account-wide regardless of it (GetCostAndUsage isn't filtered by REGION
    here), so this doesn't limit which regions' spend gets counted. CloudWatch
    metrics, unlike CE cost data, *are* regional, so `cloudwatch_regions`
    (defaulting to `[region]` when omitted) lets multiple regions be searched
    per usage-based resource — see enrich_with_cloudwatch's docstring for how
    ties/misses across regions are handled.
    """
    if force_refresh:
        from .cache import invalidate
        start, end = _date_range(lookback_days)
        invalidate(cache_key("ce_actuals_by_account_service", profile or "default", region, start, end))
        today = date.today()
        invalidate(cache_key("ce_forecast", profile or "default", region,
                             today.strftime("%Y-%m-%d"),
                             (today + timedelta(days=30)).strftime("%Y-%m-%d")))
        invalidate(cache_key("org_account_aliases", profile or "default"))

    ce = _ce_client(profile, region)
    start, end = _date_range(lookback_days)

    by_account_service = _get_actuals_by_account_service(
        ce, start, end, profile, region, cache_ttl_days
    )
    actuals_by_service = _aggregate_by_service(by_account_service)
    actuals_by_account = _account_totals(by_account_service)

    forecast = _get_forecast(ce, profile, region, cache_ttl_days)
    account_aliases = _account_alias_map(profile, cache_ttl_days)

    months = max(lookback_days / 30, 1)
    monthly_actuals = {k: v / months for k, v in actuals_by_service.items()}
    monthly_by_account = {a: v / months for a, v in actuals_by_account.items()}

    if len(actuals_by_account) > 1:
        print(f"  [aws] {len(actuals_by_account)} accounts detected in consolidated billing")

    result = {
        "version": output.version,
        "currency": output.currency,
        "timeGenerated": output.time_generated,
        "totalMonthlyCost": output.total_monthly_cost,
        "historical": {
            "lookbackDays": lookback_days,
            "start": start,
            "end": end,
            "accounts": sorted(by_account_service.keys()),
            "actualsByService": actuals_by_service,
            "monthlyAverageByService": monthly_actuals,
            "actualsByAccount": actuals_by_account,
            "monthlyAverageByAccount": monthly_by_account,
            "actualsByAccountService": by_account_service,
            "accountAliases": account_aliases,
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
            region=cloudwatch_regions or [region],
            lookback_days=lookback_days,
            ttl_days=cache_ttl_days,
        )
        if cw_actuals:
            print(f"  [cloudwatch] enriched {len(cw_actuals)} resources")

    # Usage-based cost estimates (CW actuals × unit price from infracost JSON)
    estimates: dict[str, float] = {}
    if cw_actuals:
        from ..pricing.estimator import estimate_all
        all_resources = [
            r
            for p in output.projects if p.breakdown
            for r in p.breakdown.resources
        ]
        estimates = estimate_all(all_resources, cw_actuals, lookback_days)
        if estimates:
            print(f"  [estimate] computed estimates for {len(estimates)} resources")

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
                    _enrich_resource(r, monthly_actuals, cw_actuals, estimates)
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
    estimates: dict[str, float] | None = None,
) -> dict:
    svc = resource.aws_service()
    aws_svc_name = _SVC_MAP.get(svc)
    actual_monthly = actuals_by_service.get(aws_svc_name) if aws_svc_name else None
    cw = (cw_actuals or {}).get(resource.name, {})
    estimated_cost = (estimates or {}).get(resource.name)

    return {
        "name": resource.name,
        "resourceType": resource.resource_type,
        "tags": resource.tags,
        "monthlyCost": resource.total_monthly_cost(),
        "awsService": svc,
        "estimatedMonthlyCost": estimated_cost,
        "historical": {
            "actualMonthlyServiceTotal": actual_monthly,
            "cloudwatchActuals": cw or None,
        },
    }
