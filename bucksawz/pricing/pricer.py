"""
Map parsed terraform resource configs directly to priced Resources using the
local SQLite price cache, bypassing Infracost entirely.

Resources that can't be matched to a price (unsupported type, missing
attribute, no cached price for the region) come back as unpriced (`no_price`)
rather than being dropped, so the report still lists them.
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional

from . import db as price_db
from .tf_state import TFResource
from ..schema.infracost import Breakdown, CostComponent, InfracostOutput, Project, Resource

_ENGINE_MAP = {
    "mysql": "MySQL",
    "postgres": "PostgreSQL",
    "postgresql": "PostgreSQL",
    "aurora-mysql": "Aurora MySQL",
    "aurora-postgresql": "Aurora PostgreSQL",
}

# Not sourced from the Pricing API cache (no ELB fetcher yet) — flat approximate
# on-demand hourly rates, same across regions. LCU rate matches infracost's own.
_ELB_HOURLY_RATE = {
    "application": 0.0225,
    "network": 0.0225,
    "gateway": 0.0125,
    "classic": 0.025,
}
_ELB_LCU_PRICE = 0.008


def _unpriced(tf: TFResource, reason: str) -> Resource:
    return Resource(
        name=tf.address,
        resource_type=tf.type,
        tags=tf.values.get("tags") or {},
        monthly_cost=None,
        hourly_cost=None,
        cost_components=[],
        sub_resources=[],
        is_supported=False,
        no_price=True,
    )


def _price_ec2_instance(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    values = tf.values
    instance_type = values.get("instance_type")
    if not instance_type:
        return _unpriced(tf, "missing instance_type")
    row = price_db.get_price("AmazonEC2", region, f"ec2:{instance_type}:linux:shared", db=db)
    if row is None:
        return _unpriced(tf, f"no price data for {instance_type} in {region}")
    price = row["price_usd"]
    monthly_cost = price * 730
    comp = CostComponent(
        name=f"Instance usage (Linux/UNIX, on-demand, {instance_type})",
        unit="hours",
        hourly_quantity=1.0,
        monthly_quantity=730.0,
        price=price,
        hourly_cost=price,
        monthly_cost=monthly_cost,
        usage_based=False,
    )
    return Resource(
        name=tf.address,
        resource_type=tf.type,
        tags=values.get("tags") or {},
        monthly_cost=monthly_cost,
        hourly_cost=price,
        cost_components=[comp],
        sub_resources=[],
    )


def _price_rds_instance(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    values = tf.values
    instance_class = values.get("instance_class")
    engine = _ENGINE_MAP.get((values.get("engine") or "").lower())
    if not instance_class or not engine:
        return _unpriced(tf, f"unsupported engine '{values.get('engine')}'")
    deployment = "Multi-AZ" if values.get("multi_az") else "Single-AZ"
    row = price_db.get_price("AmazonRDS", region, f"rds:{instance_class}:{engine}:{deployment}", db=db)
    if row is None:
        # aurora cluster instances don't carry multi_az at the instance level
        row = price_db.get_price("AmazonRDS", region, f"rds:{instance_class}:{engine}:Single-AZ", db=db)
    if row is None:
        return _unpriced(tf, f"no price data for {instance_class}/{engine}/{deployment} in {region}")
    price = row["price_usd"]
    monthly_cost = price * 730
    comp = CostComponent(
        name=f"Database instance ({instance_class}, {deployment.lower()})",
        unit="hours",
        hourly_quantity=1.0,
        monthly_quantity=730.0,
        price=price,
        hourly_cost=price,
        monthly_cost=monthly_cost,
        usage_based=False,
    )
    return Resource(
        name=tf.address,
        resource_type=tf.type,
        tags=values.get("tags") or {},
        monthly_cost=monthly_cost,
        hourly_cost=price,
        cost_components=[comp],
        sub_resources=[],
    )


def _price_lb(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    values = tf.values
    lb_type = (values.get("load_balancer_type") or "application").lower()
    rate = _ELB_HOURLY_RATE.get(lb_type, _ELB_HOURLY_RATE["application"])
    monthly_cost = rate * 730
    fixed_comp = CostComponent(
        name=f"{lb_type.capitalize()} load balancer",
        unit="hours",
        hourly_quantity=1.0,
        monthly_quantity=730.0,
        price=rate,
        hourly_cost=rate,
        monthly_cost=monthly_cost,
        usage_based=False,
    )
    lcu_comp = CostComponent(
        name="Load balancer capacity units",
        unit="LCU",
        hourly_quantity=None,
        monthly_quantity=None,
        price=_ELB_LCU_PRICE,
        hourly_cost=None,
        monthly_cost=None,
        usage_based=True,
    )
    return Resource(
        name=tf.address,
        resource_type=tf.type,
        tags=values.get("tags") or {},
        monthly_cost=monthly_cost,
        hourly_cost=rate,
        cost_components=[fixed_comp, lcu_comp],
        sub_resources=[],
    )


def _price_ecs_task(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    values = tf.values
    compat = [c.upper() for c in (values.get("requires_compatibilities") or [])]
    if "FARGATE" not in compat:
        return None  # EC2-backed ECS cost is captured via the underlying aws_instance

    cpu_raw = values.get("cpu")
    mem_raw = values.get("memory")
    if not cpu_raw or not mem_raw:
        return _unpriced(tf, "missing cpu/memory on Fargate task definition")
    vcpu = float(cpu_raw) / 1024.0
    memory_gb = float(mem_raw) / 1024.0

    runtime_platform = values.get("runtime_platform") or []
    if isinstance(runtime_platform, dict):
        runtime_platform = [runtime_platform]
    arch = "X86_64"
    if runtime_platform:
        arch = (runtime_platform[0].get("cpu_architecture") or "X86_64").upper()
    is_arm = arch == "ARM64"

    vcpu_row = price_db.get_price("AmazonECS", region, "fargate:vcpu:arm" if is_arm else "fargate:vcpu", db=db)
    mem_row = price_db.get_price("AmazonECS", region, "fargate:memory:arm" if is_arm else "fargate:memory", db=db)
    if vcpu_row is None or mem_row is None:
        return _unpriced(tf, f"no Fargate price data ({'ARM' if is_arm else 'x86'}) in {region}")

    vcpu_price = vcpu_row["price_usd"]
    mem_price = mem_row["price_usd"]
    vcpu_monthly = vcpu * 730 * vcpu_price
    mem_monthly = memory_gb * 730 * mem_price
    # Assumes one continuously-running task per task definition (no aws_ecs_service
    # desired_count is available on the task definition itself).
    comps = [
        CostComponent(
            name="Fargate vCPU hours",
            unit="vCPU-hours",
            hourly_quantity=vcpu,
            monthly_quantity=vcpu * 730,
            price=vcpu_price,
            hourly_cost=vcpu * vcpu_price,
            monthly_cost=vcpu_monthly,
            usage_based=False,
        ),
        CostComponent(
            name="Fargate GB hours",
            unit="GB-hours",
            hourly_quantity=memory_gb,
            monthly_quantity=memory_gb * 730,
            price=mem_price,
            hourly_cost=memory_gb * mem_price,
            monthly_cost=mem_monthly,
            usage_based=False,
        ),
    ]
    monthly_cost = vcpu_monthly + mem_monthly
    return Resource(
        name=tf.address,
        resource_type=tf.type,
        tags=values.get("tags") or {},
        monthly_cost=monthly_cost,
        hourly_cost=monthly_cost / 730,
        cost_components=comps,
        sub_resources=[],
    )


def _price_lambda(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    values = tf.values
    arch_list = values.get("architectures") or ["x86_64"]
    arch = (arch_list[0] if arch_list else "x86_64").lower().replace(" ", "_")

    dur_row = price_db.get_price("AWSLambda", region, f"lambda:duration:{arch}", db=db)
    req_row = price_db.get_price("AWSLambda", region, "lambda:requests", db=db)
    if dur_row is None or req_row is None:
        return _unpriced(tf, f"no Lambda price data ({arch}) in {region}")

    # Usage-based, like infracost's own output: no monthly_cost until enriched
    # with CloudWatch invocation/duration actuals (see estimator.py).
    duration_comp = CostComponent(
        name=f"Duration ({arch})",
        unit="GB-seconds",
        hourly_quantity=None,
        monthly_quantity=None,
        price=dur_row["price_usd"],
        hourly_cost=None,
        monthly_cost=None,
        usage_based=True,
    )
    requests_comp = CostComponent(
        name="Requests",
        unit="1M requests",
        hourly_quantity=None,
        monthly_quantity=None,
        price=req_row["price_usd"],
        hourly_cost=None,
        monthly_cost=None,
        usage_based=True,
    )
    return Resource(
        name=tf.address,
        resource_type=tf.type,
        tags=values.get("tags") or {},
        monthly_cost=None,
        hourly_cost=None,
        cost_components=[duration_comp, requests_comp],
        sub_resources=[],
    )


_PRICERS = {
    "aws_instance": _price_ec2_instance,
    "aws_launch_template": _price_ec2_instance,
    "aws_db_instance": _price_rds_instance,
    "aws_rds_cluster_instance": _price_rds_instance,
    "aws_lb": _price_lb,
    "aws_ecs_task_definition": _price_ecs_task,
    "aws_lambda_function": _price_lambda,
}


def price_resources(resources: list[TFResource], region: str, db=None) -> list[Resource]:
    """Price every supported resource; unsupported types are skipped entirely."""
    priced: list[Resource] = []
    for tf in resources:
        fn = _PRICERS.get(tf.type)
        if fn is None:
            continue
        result = fn(tf, region, db)
        if result is not None:
            priced.append(result)
    return priced


def build_output(resources: list[Resource], region: str) -> InfracostOutput:
    """Wrap priced resources in the same InfracostOutput schema `report` consumes."""
    total_monthly = sum(r.total_monthly_cost() for r in resources)
    breakdown = Breakdown(
        resources=resources,
        total_hourly_cost=total_monthly / 730 if total_monthly else 0.0,
        total_monthly_cost=total_monthly,
    )
    project = Project(
        name=f"terraform-state-{region}",
        metadata={"path": "terraform show -json", "type": "terraform_state"},
        past_breakdown=None,
        breakdown=breakdown,
        diff=None,
        summary={},
    )
    supported = sum(1 for r in resources if r.is_supported)
    no_price = sum(1 for r in resources if r.no_price)
    return InfracostOutput(
        version="bucksawz-price-state-1",
        currency="USD",
        projects=[project],
        total_hourly_cost=breakdown.total_hourly_cost,
        total_monthly_cost=total_monthly,
        time_generated=datetime.now(timezone.utc).isoformat(),
        summary={
            "totalDetectedResources": len(resources),
            "totalSupportedResources": supported,
            "totalNoPriceResources": no_price,
            "totalUnsupportedResources": len(resources) - supported - no_price,
        },
    )
