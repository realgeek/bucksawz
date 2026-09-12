"""
AWS Cost Explorer enrichment.
Pulls GetCostAndUsage for the lookback window and merges actuals into
the infracost output, filling in usage-based cost estimates.

Results are cached locally for 7 days (configurable via --cache-ttl).
Cache lives in ~/.cache/bucksawz/. Override with $BUCKSAWZ_CACHE_DIR.
"""
from __future__ import annotations
import json
from calendar import monthrange
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


def _today() -> date:
    return date.today()


def _date_range(lookback_days: int) -> tuple[str, str]:
    end = date.today()
    start = end - timedelta(days=lookback_days)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _paginate_cost_and_usage(ce, **kwargs):
    """
    GetCostAndUsage has no botocore paginator config (it's absent from
    ce/paginators-1.json — confirmed against botocore 1.43), so
    ce.get_paginator("get_cost_and_usage") raises OperationNotPageableError.
    Page it by hand via NextPageToken instead, yielding each raw response
    the same way a botocore paginator page would look.
    """
    token = None
    while True:
        call_kwargs = dict(kwargs)
        if token:
            call_kwargs["NextPageToken"] = token
        page = ce.get_cost_and_usage(**call_kwargs)
        yield page
        token = page.get("NextPageToken")
        if not token:
            break


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
    for page in _paginate_cost_and_usage(
        ce,
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


def _sum_data_transfer_usage(ce, start: str, end: str, region: str) -> tuple[float, float, bool]:
    """
    Raw (un-averaged) egress/inter-AZ GB over [start, end) from Cost
    Explorer. Filters to SERVICE "EC2 - Other" (where AWS buckets
    data-transfer line items) and REGION, grouped by USAGE_TYPE. Classifies
    by usage-type suffix the same way fetch_data_transfer (fetcher.py)
    classifies the Pricing API's `transferType`: "-Out-Bytes" (excluding
    "In-Bytes") is internet egress, "-Regional-Bytes" is inter-AZ.

    NOTE: written without live Cost Explorer access (no AWS credentials in
    the dev sandbox) — the usage-type suffixes are inferred from published
    CUR column documentation, not verified against a real GetCostAndUsage
    response. Validate against `aws ce get-cost-and-usage` output for a real
    account with data-transfer spend before trusting the numbers, and adjust
    the suffixes here if they don't match.
    """
    egress_gb = 0.0
    inter_az_gb = 0.0
    found = False

    for page in _paginate_cost_and_usage(
        ce,
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

    return egress_gb, inter_az_gb, found


def fetch_data_transfer_actuals(
    lookback_days: int,
    profile: Optional[str],
    region: str,
    cache_ttl_days: int = _DEFAULT_TTL_DAYS,
) -> Optional[dict[str, float]]:
    """
    Real monthly internet-egress and inter-AZ data-transfer volume from Cost
    Explorer, averaged over a fixed trailing window — as an alternative to
    `--usage-file` when `price-state` has AWS account access. See
    pricer.estimate_data_transfer_cost, which accepts either source through
    the same {internet_egress_gb_month, inter_az_gb_month} shape used by
    usage_file.data_transfer_usage.

    Prefer `fetch_data_transfer_estimate` for a current-month estimate: it
    picks the most stable window automatically (3-month average when
    available) instead of a single fixed lookback. This function remains for
    callers that want a specific, fixed-length window instead.

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
    egress_gb, inter_az_gb, found = _sum_data_transfer_usage(ce, start, end, region)
    if not found:
        return None

    result = {
        "internet_egress_gb_month": egress_gb / months,
        "inter_az_gb_month": inter_az_gb / months,
    }
    cache_put(key, result)
    return result


def _first_of_month(d: date) -> date:
    return d.replace(day=1)


def _months_before(d: date, n: int) -> date:
    """First-of-month date `n` months before `d`'s month."""
    month = d.month - n
    year = d.year
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1)


def fetch_data_transfer_estimate(
    profile: Optional[str],
    region: str,
    cache_ttl_days: int = _DEFAULT_TTL_DAYS,
) -> Optional[dict[str, float]]:
    """
    Best available monthly egress/inter-AZ estimate for the *current* month,
    picking the most stable source Cost Explorer can actually supply:

      1. Average of the prior 3 complete calendar months — smooths out a
         one-off spike or lull in any single month. Used whenever Cost
         Explorer has data-transfer usage anywhere in that window.
      2. The trailing 30 days, when there isn't 3 months of history yet
         (e.g. a newer account or region).
      3. Month-to-date, extrapolated to a full month by elapsed-day
         fraction, as a last resort (e.g. the account's first few days).

    Returns None only if none of the three windows found any data-transfer
    usage at all, so callers can fall back to `--usage-file`.
    """
    ce = _ce_client(profile, region)
    today = _today()
    three_months_start = _months_before(today, 3)
    this_month_start = _first_of_month(today)

    key = cache_key(
        "ce_data_transfer_estimate", profile or "default", region,
        three_months_start.isoformat(), today.isoformat(),
    )
    cached = cache_get(key, ttl_days=cache_ttl_days)
    if cached is not None:
        print(f"  [cache hit] data_transfer_estimate ({cached.get('source', '?')})")
        return {k: v for k, v in cached.items() if k != "source"}

    print("  [aws] fetching Cost Explorer data-transfer usage for a current-month estimate…")

    egress_gb, inter_az_gb, found = _sum_data_transfer_usage(
        ce, three_months_start.isoformat(), this_month_start.isoformat(), region,
    )
    if found:
        result = {
            "internet_egress_gb_month": egress_gb / 3,
            "inter_az_gb_month": inter_az_gb / 3,
            "source": "average of the prior 3 full months",
        }
    else:
        thirty_days_ago = (today - timedelta(days=30)).isoformat()
        egress_gb, inter_az_gb, found = _sum_data_transfer_usage(
            ce, thirty_days_ago, today.isoformat(), region,
        )
        if found:
            result = {
                "internet_egress_gb_month": egress_gb,
                "inter_az_gb_month": inter_az_gb,
                "source": "trailing 30 days (not enough history for a 3-month average)",
            }
        else:
            elapsed_days = (today - this_month_start).days or 1
            days_in_month = monthrange(today.year, today.month)[1]
            egress_gb, inter_az_gb, found = _sum_data_transfer_usage(
                ce, this_month_start.isoformat(), today.isoformat(), region,
            )
            if not found:
                return None
            scale = days_in_month / elapsed_days
            result = {
                "internet_egress_gb_month": egress_gb * scale,
                "inter_az_gb_month": inter_az_gb * scale,
                "source": "month-to-date, extrapolated to a full month",
            }

    print(f"  using {result['source']}")
    cache_put(key, result)
    return {k: v for k, v in result.items() if k != "source"}


def _fetch_usage_by_type(
    cache_name: str,
    service: str,
    lookback_days: int,
    profile: Optional[str],
    region: str,
    cache_ttl_days: int,
) -> Optional[dict[str, float]]:
    """
    Shared plumbing for the CE-actuals fetchers below: paginate
    GetCostAndUsage for `service` in `region`, grouped by USAGE_TYPE. Returns
    the raw {usage_type: monthly_average_quantity} map, or None if the
    service had no usage in the lookback window. Caching and the
    classify-by-usage-type-substring step are left to each caller, since
    those differ per service.
    """
    ce = _ce_client(profile, region)
    start, end = _date_range(lookback_days)

    key = cache_key(cache_name, profile or "default", region, start, end)
    cached = cache_get(key, ttl_days=cache_ttl_days)
    if cached is not None:
        print(f"  [cache hit] {cache_name} ({start}→{end})")
        return cached

    print(f"  [aws] fetching Cost Explorer {cache_name} ({start}→{end})…")
    months = max(lookback_days / 30, 1)
    totals: dict[str, float] = {}

    for page in _paginate_cost_and_usage(
        ce,
        TimePeriod={"Start": start, "End": end},
        Granularity="MONTHLY",
        Metrics=["UsageQuantity"],
        Filter={
            "And": [
                {"Dimensions": {"Key": "SERVICE", "Values": [service]}},
                {"Dimensions": {"Key": "REGION", "Values": [region]}},
            ]
        },
        GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
    ):
        for period in page.get("ResultsByTime", []):
            for group in period.get("Groups", []):
                [usage_type] = group["Keys"]
                qty = float(group["Metrics"]["UsageQuantity"]["Amount"])
                totals[usage_type] = totals.get(usage_type, 0.0) + qty / months

    if not totals:
        return None
    cache_put(key, totals)
    return totals


def fetch_s3_storage_actuals(
    lookback_days: int,
    profile: Optional[str],
    region: str,
    cache_ttl_days: int = _DEFAULT_TTL_DAYS,
) -> Optional[dict[str, float]]:
    """
    Real average Standard-class storage volume from Cost Explorer, to feed
    pricer._price_s3_bucket's usage-based storage component the same way
    fetch_data_transfer_actuals feeds price_data_transfer.

    Filters to SERVICE "Amazon Simple Storage Service", matching only the
    plain "TimedStorage-ByteHrs" usage-type suffix — other storage classes
    (Standard-IA, Glacier, Intelligent-Tiering, ...) use a different infix
    ("TimedStorage-SIA-ByteHrs" etc.) and are excluded, matching
    _price_s3_bucket's "Standard-class only" scope. Cost Explorer already
    reports this metric as a GB-month average, not byte-hours needing
    conversion, so no extra normalization is applied here.

    NOTE: written without live Cost Explorer access — validate the usage-type
    suffix against a real account with S3 spend before trusting the numbers.
    """
    totals = _fetch_usage_by_type(
        "ce_s3_storage_usage", "Amazon Simple Storage Service",
        lookback_days, profile, region, cache_ttl_days,
    )
    if totals is None:
        return None
    storage_gb = sum(v for k, v in totals.items() if k.endswith("TimedStorage-ByteHrs"))
    if storage_gb <= 0:
        return None
    return {"storage_gb": storage_gb}


def fetch_elb_usage_actuals(
    lookback_days: int,
    profile: Optional[str],
    region: str,
    cache_ttl_days: int = _DEFAULT_TTL_DAYS,
) -> Optional[dict[str, float]]:
    """
    Real average load-balancer usage from Cost Explorer: LCU-hours for
    ALB/NLB (usage type containing "LCUUsage"), to feed pricer._price_lb's
    "Load balancer capacity units" component.

    NOTE: written without live Cost Explorer access — validate the usage-type
    substring against a real account with ELB spend before trusting the
    numbers.
    """
    totals = _fetch_usage_by_type(
        "ce_elb_usage", "Amazon Elastic Load Balancing",
        lookback_days, profile, region, cache_ttl_days,
    )
    if totals is None:
        return None
    lcu_hours = sum(v for k, v in totals.items() if "LCUUsage" in k)
    if lcu_hours <= 0:
        return None
    return {"lcu_hours_month": lcu_hours}


def fetch_rds_storage_actuals(
    lookback_days: int,
    profile: Optional[str],
    region: str,
    cache_ttl_days: int = _DEFAULT_TTL_DAYS,
) -> Optional[dict[str, float]]:
    """
    Real average Aurora storage volume from Cost Explorer (usage type
    containing "Aurora:StorageUsage"), to feed a usage-based Aurora storage
    component in pricer._price_rds_instance. Standard (non-Aurora) RDS
    storage is provisioned/flat and already derivable from `allocated_storage`
    in the terraform config, so it isn't covered here.

    NOTE: written without live Cost Explorer access — validate the usage-type
    substring against a real account with Aurora spend before trusting the
    numbers.
    """
    totals = _fetch_usage_by_type(
        "ce_rds_storage_usage", "Amazon Relational Database Service",
        lookback_days, profile, region, cache_ttl_days,
    )
    if totals is None:
        return None
    storage_gb = sum(v for k, v in totals.items() if "Aurora:StorageUsage" in k)
    if storage_gb <= 0:
        return None
    return {"storage_gb": storage_gb}


def fetch_elasticache_runtime_actuals(
    lookback_days: int,
    profile: Optional[str],
    region: str,
    cache_ttl_days: int = _DEFAULT_TTL_DAYS,
) -> Optional[dict[str, float]]:
    """
    Real average node run-hours from Cost Explorer (usage type containing
    "NodeUsage"), to replace pricer._price_elasticache's flat 24/7 (730h)
    assumption for clusters that aren't always running.

    NOTE: written without live Cost Explorer access — validate the usage-type
    substring against a real account with ElastiCache spend before trusting
    the numbers.
    """
    totals = _fetch_usage_by_type(
        "ce_elasticache_usage", "Amazon ElastiCache",
        lookback_days, profile, region, cache_ttl_days,
    )
    if totals is None:
        return None
    node_hours = sum(v for k, v in totals.items() if "NodeUsage" in k)
    if node_hours <= 0:
        return None
    return {"node_hours_month": node_hours}


def fetch_ec2_runtime_actuals(
    lookback_days: int,
    profile: Optional[str],
    region: str,
    cache_ttl_days: int = _DEFAULT_TTL_DAYS,
) -> Optional[dict[str, float]]:
    """
    Real average on-demand instance run-hours from Cost Explorer (usage type
    containing "BoxUsage" — excludes Spot/Reserved/Dedicated variants, which
    use different usage-type prefixes), to replace pricer._price_ec2_instance's
    flat 24/7 (730h) assumption when actual runtime is available. Falls back
    to the 24/7 assumption (the existing default) when this returns None.

    NOTE: written without live Cost Explorer access — validate the usage-type
    substring against a real account with EC2 spend before trusting the
    numbers.
    """
    totals = _fetch_usage_by_type(
        "ce_ec2_runtime_usage", "Amazon Elastic Compute Cloud - Compute",
        lookback_days, profile, region, cache_ttl_days,
    )
    if totals is None:
        return None
    instance_hours = sum(v for k, v in totals.items() if "BoxUsage" in k)
    if instance_hours <= 0:
        return None
    return {"instance_hours_month": instance_hours}


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
