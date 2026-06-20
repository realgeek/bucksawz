"""
CloudWatch metric enrichment for usage-based cost components.

Fetches p50 actual usage over the lookback period for resources whose
cost depends on usage (ALB LCU, SQS messages, Lambda invocations, etc.)
and returns per-resource usage quantities that can fill in estimates.

Resource matching strategy: best-effort by Terraform resource name →
AWS resource name heuristic, falling back to tag-based lookup.
Results cached alongside Cost Explorer data (same TTL).
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import Optional
import boto3
from .cache import get as cache_get, put as cache_put, cache_key

# CloudWatch metric definitions per resource type.
# Each entry: (namespace, metric_name, stat, unit_divisor, cw_dimensions_fn)
# cw_dimensions_fn receives the resource name and returns [{Name, Value}] or None.
_METRIC_DEFS: dict[str, dict] = {
    "aws_lb": {
        "alb": {
            "namespace": "AWS/ApplicationELB",
            "metric": "ConsumedLCUs",
            "stat": "Average",
            "dim_key": "LoadBalancer",
        },
        "nlb": {
            "namespace": "AWS/NetworkELB",
            "metric": "ConsumedLCUs",
            "stat": "Average",
            "dim_key": "LoadBalancer",
        },
    },
    "aws_sqs_queue": {
        "namespace": "AWS/SQS",
        "metric": "NumberOfMessagesSent",
        "stat": "Sum",
        "dim_key": "QueueName",
    },
    "aws_lambda_function": {
        "namespace": "AWS/Lambda",
        "metric": "Invocations",
        "stat": "Sum",
        "dim_key": "FunctionName",
    },
    "aws_api_gateway_rest_api": {
        "namespace": "AWS/ApiGateway",
        "metric": "Count",
        "stat": "Sum",
        "dim_key": "ApiName",
    },
    "aws_apigatewayv2_api": {
        "namespace": "AWS/ApiGateway",
        "metric": "Count",
        "stat": "Sum",
        "dim_key": "ApiId",
    },
    "aws_cloudwatch_log_group": {
        "namespace": "AWS/Logs",
        "metric": "IncomingBytes",
        "stat": "Sum",
        "dim_key": "LogGroupName",
    },
}


def _cw_client(profile: Optional[str], region: str):
    session = boto3.Session(profile_name=profile, region_name=region)
    return session.client("cloudwatch", region_name=region)


def _elb_client(profile: Optional[str], region: str):
    session = boto3.Session(profile_name=profile, region_name=region)
    return session.client("elbv2", region_name=region)


def _resource_name_suffix(tf_name: str) -> str:
    """'aws_lb.main' → 'main', 'module.foo.aws_lb.bar' → 'bar'"""
    parts = tf_name.rsplit(".", 1)
    suffix = parts[-1] if len(parts) > 1 else tf_name
    # Strip array index: bar[0] → bar
    return suffix.split("[")[0]


def _list_alb_dimension_values(elb, region: str) -> dict[str, str]:
    """Map short ALB name → CloudWatch LoadBalancer dimension value (arn suffix)."""
    mapping = {}
    try:
        paginator = elb.get_paginator("describe_load_balancers")
        for page in paginator.paginate():
            for lb in page.get("LoadBalancers", []):
                name = lb["LoadBalancerName"]
                # CW dimension is the part of the ARN after 'loadbalancer/'
                arn = lb["LoadBalancerArn"]
                dim_val = arn.split("loadbalancer/", 1)[-1] if "loadbalancer/" in arn else name
                mapping[name.lower()] = dim_val
    except Exception:
        pass
    return mapping


def _get_metric_p50(
    cw,
    namespace: str,
    metric_name: str,
    dimensions: list[dict],
    start: datetime,
    end: datetime,
    stat: str = "Average",
    ttl_days: int = 7,
    profile: Optional[str] = None,
    region: str = "us-east-1",
) -> Optional[float]:
    key = cache_key(
        "cw_metric",
        namespace, metric_name,
        str(dimensions),
        start.date().isoformat(), end.date().isoformat(),
        profile or "default", region,
    )
    cached = cache_get(key, ttl_days=ttl_days)
    if cached is not None:
        return cached

    try:
        resp = cw.get_metric_statistics(
            Namespace=namespace,
            MetricName=metric_name,
            Dimensions=dimensions,
            StartTime=start,
            EndTime=end,
            Period=int((end - start).total_seconds()),
            Statistics=["Average", "Sum"],
        )
        points = resp.get("Datapoints", [])
        if not points:
            return None
        val = float(points[-1].get(stat, points[-1].get("Average", 0)))
        cache_put(key, val)
        return val
    except Exception:
        return None


def enrich_with_cloudwatch(
    resources: list,
    profile: Optional[str],
    region: str,
    lookback_days: int,
    ttl_days: int,
) -> dict[str, dict]:
    """
    Returns a dict keyed by resource name with CloudWatch actuals:
      { "aws_lb.main": {"ConsumedLCUs": 4.2, "unit": "LCU"}, ... }

    Only resources with usage-based components are queried.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days)

    cw = _cw_client(profile, region)

    # Pre-fetch ALB dimension values (ARN suffixes) once
    alb_dim_map: dict[str, str] = {}
    has_lb = any(
        r.resource_type in ("aws_lb", "aws_alb") or r.name.split(".")[0] in ("aws_lb", "aws_alb")
        for r in resources
        if hasattr(r, "cost_components") and any(c.usage_based for c in r.cost_components)
    )
    if has_lb:
        try:
            elb = _elb_client(profile, region)
            alb_dim_map = _list_alb_dimension_values(elb, region)
        except Exception:
            pass

    results: dict[str, dict] = {}

    for resource in resources:
        if not hasattr(resource, "cost_components"):
            continue
        if not any(c.usage_based for c in resource.cost_components):
            continue

        rt = (resource.resource_type or resource.name.split(".")[0]).lower()
        short_name = _resource_name_suffix(resource.name)
        actuals: dict[str, float] = {}

        if rt in ("aws_lb", "aws_alb"):
            # Try ALB first, fall back to NLB namespace
            dim_val = alb_dim_map.get(short_name.lower(), short_name)
            for lb_type in ("alb", "nlb"):
                defn = _METRIC_DEFS["aws_lb"][lb_type]
                val = _get_metric_p50(
                    cw=cw,
                    namespace=defn["namespace"],
                    metric_name=defn["metric"],
                    dimensions=[{"Name": defn["dim_key"], "Value": dim_val}],
                    start=start, end=end, stat=defn["stat"],
                    ttl_days=ttl_days, profile=profile, region=region,
                )
                if val is not None:
                    actuals["ConsumedLCUs"] = val
                    actuals["unit"] = "LCU"
                    break

        elif rt == "aws_sqs_queue":
            defn = _METRIC_DEFS["aws_sqs_queue"]
            val = _get_metric_p50(
                cw=cw,
                namespace=defn["namespace"],
                metric_name=defn["metric"],
                dimensions=[{"Name": defn["dim_key"], "Value": short_name}],
                start=start, end=end, stat=defn["stat"],
                ttl_days=ttl_days, profile=profile, region=region,
            )
            if val is not None:
                # Convert total requests → millions for pricing unit
                actuals["Requests"] = val / 1_000_000
                actuals["unit"] = "1M requests"

        elif rt == "aws_lambda_function":
            defn = _METRIC_DEFS["aws_lambda_function"]
            val = _get_metric_p50(
                cw=cw,
                namespace=defn["namespace"],
                metric_name=defn["metric"],
                dimensions=[{"Name": defn["dim_key"], "Value": short_name}],
                start=start, end=end, stat=defn["stat"],
                ttl_days=ttl_days, profile=profile, region=region,
            )
            if val is not None:
                actuals["Invocations"] = val / 1_000_000
                actuals["unit"] = "1M requests"

        elif rt in ("aws_api_gateway_rest_api", "aws_apigatewayv2_api"):
            defn = _METRIC_DEFS.get(rt, _METRIC_DEFS["aws_api_gateway_rest_api"])
            val = _get_metric_p50(
                cw=cw,
                namespace=defn["namespace"],
                metric_name=defn["metric"],
                dimensions=[{"Name": defn["dim_key"], "Value": short_name}],
                start=start, end=end, stat=defn["stat"],
                ttl_days=ttl_days, profile=profile, region=region,
            )
            if val is not None:
                actuals["Requests"] = val / 1_000_000
                actuals["unit"] = "1M requests"

        if actuals:
            results[resource.name] = actuals

    return results
