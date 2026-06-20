"""
AWS Cost Explorer enrichment.
Pulls GetCostAndUsage for the lookback window and merges actuals into
the infracost output, filling in usage-based cost estimates.
"""
from __future__ import annotations
import json
from datetime import date, timedelta
from typing import Optional
import boto3
from ..schema.infracost import InfracostOutput, Project, Resource


def _ce_client(profile: Optional[str], region: str):
    session = boto3.Session(profile_name=profile, region_name=region)
    return session.client("ce")


def _date_range(lookback_days: int) -> tuple[str, str]:
    end = date.today()
    start = end - timedelta(days=lookback_days)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _get_actuals_by_service(ce, start: str, end: str) -> dict[str, float]:
    """Total spend by AWS service for the lookback period."""
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
    return totals


def _get_forecast(ce, end: str, currency: str = "USD") -> Optional[float]:
    """30-day forward cost forecast from today."""
    today = date.today()
    forecast_end = today + timedelta(days=30)
    try:
        resp = ce.get_cost_forecast(
            TimePeriod={
                "Start": today.strftime("%Y-%m-%d"),
                "End": forecast_end.strftime("%Y-%m-%d"),
            },
            Metric="UNBLENDED_COST",
            Granularity="MONTHLY",
        )
        return float(resp["Total"]["Amount"])
    except Exception:
        return None


def enrich_output(
    output: InfracostOutput,
    lookback_days: int = 90,
    profile: Optional[str] = None,
    region: str = "us-east-1",
) -> dict:
    """
    Returns a dict (JSON-serialisable) that extends the infracost output
    with a top-level 'historical' key containing Cost Explorer actuals.
    """
    ce = _ce_client(profile, region)
    start, end = _date_range(lookback_days)

    actuals_by_service = _get_actuals_by_service(ce, start, end)
    forecast = _get_forecast(ce, end, output.currency)

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
        },
        "projects": [],
    }

    for p in output.projects:
        proj_dict = {
            "name": p.name,
            "metadata": p.metadata,
            "monthlyCost": p.monthly_cost(),
            "resources": [],
        }
        if p.breakdown:
            for r in p.breakdown.resources:
                proj_dict["resources"].append(_enrich_resource(r, monthly_actuals))
        result["projects"].append(proj_dict)

    return result


def _enrich_resource(resource: Resource, actuals_by_service: dict[str, float]) -> dict:
    """Match a resource to its service actuals and annotate."""
    svc = resource.aws_service()
    # Map our service label back to AWS service name heuristic
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
    }
    aws_svc_name = _SVC_MAP.get(svc)
    actual_monthly = None
    if aws_svc_name:
        actual_monthly = actuals_by_service.get(aws_svc_name)

    return {
        "name": resource.name,
        "resourceType": resource.resource_type,
        "tags": resource.tags,
        "monthlyCost": resource.total_monthly_cost(),
        "awsService": svc,
        "historical": {
            "actualMonthlyServiceTotal": actual_monthly,
        },
    }
