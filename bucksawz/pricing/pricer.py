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

# Fallbacks used only when the price cache has no AWSELB rows for the region
# (i.e. `prices update --services ELB` hasn't been run): flat approximate
# us-east-1 on-demand rates. LCU rate matches infracost's own.
_ELB_HOURLY_RATE = {
    "application": 0.0225,
    "network": 0.0225,
    "gateway": 0.0125,
    "classic": 0.025,
}
_ELB_LCU_PRICE = 0.008

# Fallback used only when the price cache has no natgateway:hourly row for the
# region: flat approximate us-east-1 on-demand rate.
_NAT_GATEWAY_HOURLY_RATE = 0.045

# Fallbacks used only when the price cache has no AWSConfig rows for the
# region: AWS's publicly documented flat/first-tier rates.
_CONFIG_ITEM_PRICE = 0.003
_CONFIG_RULE_EVALUATION_PRICE = 0.001

# Fallback used only when the price cache has no cloudwatch:alarm row for the
# region: flat approximate us-east-1 on-demand rate ($0.10/alarm/mo standard
# resolution; high-resolution alarms cost more but aren't distinguishable
# from Terraform's `aws_cloudwatch_metric_alarm` config alone).
_CLOUDWATCH_ALARM_PRICE = 0.10

# Pricing API reports SQS and Lambda requests per single request, but the report
# and the CloudWatch estimator both work in millions (see estimator.py).
_PER_MILLION = 1_000_000


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
        no_price_reason=reason,
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
    subs = _ec2_block_device_resources(tf, region, db)
    monthly_cost += sum(s.total_monthly_cost() for s in subs)
    return Resource(
        name=tf.address,
        resource_type=tf.type,
        tags=values.get("tags") or {},
        monthly_cost=monthly_cost,
        hourly_cost=monthly_cost / 730,
        cost_components=[comp],
        sub_resources=subs,
    )


# AWS-fixed baselines included free in gp3's price — provisioning above these
# is what the extra IOPS/throughput price keys bill for. io1 has no free tier;
# io2's IOPS is billed in three tiers instead, with AWS-fixed boundaries.
_GP3_BASELINE_IOPS = 3_000.0
_GP3_BASELINE_MIBPS = 125.0
_IO2_IOPS_TIERS = [(32_000.0, "ebs:iops:io2:tier1"), (32_000.0, "ebs:iops:io2:tier2"), (None, "ebs:iops:io2:tier3")]


def _ebs_iops_cost(
    volume_type: str, iops: Optional[float], region: str, db=None
) -> tuple[float, Optional[CostComponent]]:
    """
    Monthly cost (and its component) from provisioned IOPS. gp2/st1/sc1/standard
    have no separate IOPS charge, so this returns (0.0, None) for them.
    """
    if not iops or volume_type not in ("gp3", "io1", "io2"):
        return 0.0, None

    if volume_type == "gp3":
        billable = max(0.0, iops - _GP3_BASELINE_IOPS)
        if billable <= 0:
            return 0.0, None
        row = price_db.get_price("AmazonEC2", region, "ebs:iops:gp3", db=db)
        if row is None:
            return 0.0, None
        cost = billable * row["price_usd"]
        return cost, CostComponent(
            name=f"Provisioned IOPS (above {_GP3_BASELINE_IOPS:.0f} baseline)",
            unit="IOPS-months", hourly_quantity=None, monthly_quantity=billable,
            price=row["price_usd"], hourly_cost=None, monthly_cost=cost, usage_based=False,
        )

    if volume_type == "io1":
        row = price_db.get_price("AmazonEC2", region, "ebs:iops:io1", db=db)
        if row is None:
            return 0.0, None
        cost = iops * row["price_usd"]
        return cost, CostComponent(
            name="Provisioned IOPS", unit="IOPS-months",
            hourly_quantity=None, monthly_quantity=iops,
            price=row["price_usd"], hourly_cost=None, monthly_cost=cost, usage_based=False,
        )

    # io2: blend across the three tiers. All three price rows come from one
    # `prices update` call, so a tier missing mid-ladder isn't a realistic case
    # in practice — if it happens, treat that band as free rather than failing
    # the whole volume.
    remaining, cost = iops, 0.0
    for cap, key in _IO2_IOPS_TIERS:
        if remaining <= 0:
            break
        band = remaining if cap is None else min(remaining, cap)
        row = price_db.get_price("AmazonEC2", region, key, db=db)
        if row is not None:
            cost += band * row["price_usd"]
        remaining -= band
    return cost, CostComponent(
        name="Provisioned IOPS (io2, tiered)", unit="IOPS-months",
        hourly_quantity=None, monthly_quantity=iops,
        price=None, hourly_cost=None, monthly_cost=cost, usage_based=False,
    )


def _ebs_throughput_cost(
    volume_type: str, throughput: Optional[float], region: str, db=None
) -> tuple[float, Optional[CostComponent]]:
    """Monthly cost from provisioned throughput above gp3's included baseline."""
    if volume_type != "gp3" or not throughput:
        return 0.0, None
    billable = max(0.0, throughput - _GP3_BASELINE_MIBPS)
    if billable <= 0:
        return 0.0, None
    row = price_db.get_price("AmazonEC2", region, "ebs:throughput:gp3", db=db)
    if row is None:
        return 0.0, None
    cost = billable * row["price_usd"]
    return cost, CostComponent(
        name=f"Provisioned throughput (above {_GP3_BASELINE_MIBPS:.0f} MiB/s baseline)",
        unit="MiBps-months", hourly_quantity=None, monthly_quantity=billable,
        price=row["price_usd"], hourly_cost=None, monthly_cost=cost, usage_based=False,
    )


def _price_ebs_block(
    name: str, resource_type: str, tags: dict, values: dict, region: str, db=None
) -> Resource:
    """
    Price one EBS volume from a normalized `values` dict: shared by the
    standalone `aws_ebs_volume` resource and the root_block_device /
    ebs_block_device / block_device_mappings[].ebs blocks nested inside
    aws_instance and aws_launch_template — same cost basis either way.
    """
    volume_type = (values.get("type") or values.get("volume_type") or "gp2").lower()
    size = values.get("size") or values.get("volume_size")
    if not size:
        return Resource(
            name=name, resource_type=resource_type, tags=tags,
            monthly_cost=None, hourly_cost=None, cost_components=[], sub_resources=[],
            is_supported=False, no_price=True,
            no_price_reason="missing size/volume_size",
        )

    storage_row = price_db.get_price("AmazonEC2", region, f"ebs:storage:{volume_type}", db=db)
    if storage_row is None:
        return Resource(
            name=name, resource_type=resource_type, tags=tags,
            monthly_cost=None, hourly_cost=None, cost_components=[], sub_resources=[],
            is_supported=False, no_price=True,
            no_price_reason=f"no EBS price data for {volume_type} in {region}",
        )

    size = float(size)
    storage_price = storage_row["price_usd"]
    storage_cost = size * storage_price
    comps = [CostComponent(
        name=f"Storage ({volume_type}, {size:.0f} GB)", unit="GB-months",
        hourly_quantity=None, monthly_quantity=size,
        price=storage_price, hourly_cost=None, monthly_cost=storage_cost, usage_based=False,
    )]

    iops = values.get("iops")
    iops_cost, iops_comp = _ebs_iops_cost(volume_type, float(iops) if iops else None, region, db)
    if iops_comp is not None:
        comps.append(iops_comp)

    throughput = values.get("throughput")
    tput_cost, tput_comp = _ebs_throughput_cost(volume_type, float(throughput) if throughput else None, region, db)
    if tput_comp is not None:
        comps.append(tput_comp)

    monthly_cost = storage_cost + iops_cost + tput_cost
    return Resource(
        name=name, resource_type=resource_type, tags=tags,
        monthly_cost=monthly_cost, hourly_cost=monthly_cost / 730,
        cost_components=comps, sub_resources=[],
    )


def _price_ebs_volume(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    return _price_ebs_block(tf.address, tf.type, tf.values.get("tags") or {}, tf.values, region, db)


def _ec2_block_device_resources(tf: TFResource, region: str, db=None) -> list[Resource]:
    """
    EBS volumes attached to an aws_instance or aws_launch_template, priced as
    sub-resources of the instance. Covers both the aws_instance shape
    (root_block_device / ebs_block_device) and the launch-template shape
    (block_device_mappings[].ebs) — the two providers describe the same thing
    differently. Ephemeral / no_device mappings (no `ebs` block) aren't EBS
    and are skipped.
    """
    values = tf.values
    subs: list[Resource] = []

    root = values.get("root_block_device")
    if isinstance(root, dict):
        root = [root]
    for blk in root or []:
        subs.append(_price_ebs_block(f"{tf.address} root volume", "aws_ebs_volume", {}, blk, region, db))

    for blk in values.get("ebs_block_device") or []:
        dev = blk.get("device_name", "?")
        subs.append(_price_ebs_block(f"{tf.address} block device ({dev})", "aws_ebs_volume", {}, blk, region, db))

    for mapping in values.get("block_device_mappings") or []:
        ebs = mapping.get("ebs")
        if isinstance(ebs, list):
            ebs = ebs[0] if ebs else None
        if not ebs:
            continue
        dev = mapping.get("device_name", "?")
        subs.append(_price_ebs_block(f"{tf.address} block device ({dev})", "aws_ebs_volume", {}, ebs, region, db))

    return subs


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
    if tf.type == "aws_elb":
        lb_type = "classic"  # the classic-ELB resource has no load_balancer_type
    else:
        lb_type = (values.get("load_balancer_type") or "application").lower()
        if lb_type not in _ELB_HOURLY_RATE:
            lb_type = "application"

    hourly_row = price_db.get_price("AWSELB", region, f"elb:hourly:{lb_type}", db=db)
    rate = hourly_row["price_usd"] if hourly_row else _ELB_HOURLY_RATE[lb_type]
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

    # Classic LBs bill data processed; the others bill LCUs.
    if lb_type == "classic":
        data_row = price_db.get_price("AWSELB", region, "elb:data:classic", db=db)
        variable_comp = CostComponent(
            name="Data processed",
            unit="GB",
            hourly_quantity=None,
            monthly_quantity=None,
            price=data_row["price_usd"] if data_row else None,
            hourly_cost=None,
            monthly_cost=None,
            usage_based=True,
        )
    else:
        lcu_row = price_db.get_price("AWSELB", region, f"elb:lcu:{lb_type}", db=db)
        variable_comp = CostComponent(
            name="Load balancer capacity units",
            unit="LCU",
            hourly_quantity=None,
            monthly_quantity=None,
            price=lcu_row["price_usd"] if lcu_row else _ELB_LCU_PRICE,
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
        cost_components=[fixed_comp, variable_comp],
        sub_resources=[],
    )


def _price_nat_gateway(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    values = tf.values
    hourly_row = price_db.get_price("AmazonEC2", region, "natgateway:hourly", db=db)
    rate = hourly_row["price_usd"] if hourly_row else _NAT_GATEWAY_HOURLY_RATE
    monthly_cost = rate * 730
    fixed_comp = CostComponent(
        name="NAT gateway",
        unit="hours",
        hourly_quantity=1.0,
        monthly_quantity=730.0,
        price=rate,
        hourly_cost=rate,
        monthly_cost=monthly_cost,
        usage_based=False,
    )

    data_row = price_db.get_price("AmazonEC2", region, "natgateway:data", db=db)
    variable_comp = CostComponent(
        name="Data processed",
        unit="GB",
        hourly_quantity=None,
        monthly_quantity=None,
        price=data_row["price_usd"] if data_row else None,
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
        cost_components=[fixed_comp, variable_comp],
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
        price=req_row["price_usd"] * _PER_MILLION,
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


def _elasticache_node_count(values: dict) -> int:
    """
    Nodes billed for this resource. `aws_elasticache_cluster` uses
    num_cache_nodes; replication groups use either num_cache_clusters or, in
    cluster mode, num_node_groups × (1 primary + replicas_per_node_group).
    """
    for key in ("num_cache_nodes", "num_cache_clusters"):
        count = values.get(key)
        if count:
            return int(count)
    node_groups = values.get("num_node_groups")
    if node_groups:
        return int(node_groups) * (1 + int(values.get("replicas_per_node_group") or 0))
    return 1


def _price_elasticache(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    values = tf.values
    node_type = values.get("node_type")
    if not node_type:
        return _unpriced(tf, "missing node_type")
    # Both cluster and replication-group resources default to redis when unset.
    engine = (values.get("engine") or "redis").lower()
    row = price_db.get_price("AmazonElastiCache", region, f"elasticache:{node_type}:{engine}", db=db)
    if row is None:
        return _unpriced(tf, f"no price data for {node_type}/{engine} in {region}")

    nodes = _elasticache_node_count(values)
    price = row["price_usd"]
    hourly_cost = price * nodes
    monthly_cost = hourly_cost * 730
    comp = CostComponent(
        name=f"Cache node ({node_type}, {engine})",
        unit="hours",
        hourly_quantity=float(nodes),
        monthly_quantity=nodes * 730.0,
        price=price,
        hourly_cost=hourly_cost,
        monthly_cost=monthly_cost,
        usage_based=False,
    )
    return Resource(
        name=tf.address,
        resource_type=tf.type,
        tags=values.get("tags") or {},
        monthly_cost=monthly_cost,
        hourly_cost=hourly_cost,
        cost_components=[comp],
        sub_resources=[],
    )


def _price_s3_bucket(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """
    Standard-class storage only, and always usage-based: a bucket's size isn't
    knowable from its terraform config, and lifecycle transitions to other
    classes would need the object age distribution to model.
    """
    row = price_db.get_price("AmazonS3", region, "s3:storage:standard", db=db)
    if row is None:
        return _unpriced(tf, f"no S3 storage price data in {region}")
    comp = CostComponent(
        name="Standard storage",
        unit="GB-months",
        hourly_quantity=None,
        monthly_quantity=None,
        price=row["price_usd"],
        hourly_cost=None,
        monthly_cost=None,
        usage_based=True,
    )
    return Resource(
        name=tf.address,
        resource_type=tf.type,
        tags=tf.values.get("tags") or {},
        monthly_cost=None,
        hourly_cost=None,
        cost_components=[comp],
        sub_resources=[],
    )


def _price_config_recorder(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """
    aws_config_configuration_recorder: fully usage-based, same as S3/SQS —
    how often tracked resources change (and thus how many configuration
    items get recorded) isn't knowable from the recorder's own config.
    """
    row = price_db.get_price("AWSConfig", region, "config:item", db=db)
    price = row["price_usd"] if row else _CONFIG_ITEM_PRICE
    comp = CostComponent(
        name="Configuration items recorded",
        unit="items",
        hourly_quantity=None,
        monthly_quantity=None,
        price=price,
        hourly_cost=None,
        monthly_cost=None,
        usage_based=True,
    )
    return Resource(
        name=tf.address,
        resource_type=tf.type,
        tags=tf.values.get("tags") or {},
        monthly_cost=None,
        hourly_cost=None,
        cost_components=[comp],
        sub_resources=[],
    )


def _price_config_rule(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """
    aws_config_config_rule: fully usage-based, same idea as the recorder —
    evaluation count is driven by resource change events, not by the rule's
    config (managed vs custom doesn't change the per-evaluation rate).
    """
    row = price_db.get_price("AWSConfig", region, "config:rule:evaluation", db=db)
    price = row["price_usd"] if row else _CONFIG_RULE_EVALUATION_PRICE
    comp = CostComponent(
        name="Rule evaluations",
        unit="evaluations",
        hourly_quantity=None,
        monthly_quantity=None,
        price=price,
        hourly_cost=None,
        monthly_cost=None,
        usage_based=True,
    )
    return Resource(
        name=tf.address,
        resource_type=tf.type,
        tags=tf.values.get("tags") or {},
        monthly_cost=None,
        hourly_cost=None,
        cost_components=[comp],
        sub_resources=[],
    )


def _price_cloudwatch_alarm(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """
    A metric alarm's per-month price is fixed regardless of its config
    (which metric it watches, its threshold, etc. don't change the rate),
    so unlike CloudWatch Logs this always has a known monthly_cost —
    same shape as Secrets Manager's flat per-secret rate.
    """
    row = price_db.get_price("AmazonCloudWatch", region, "cloudwatch:alarm", db=db)
    price = row["price_usd"] if row else _CLOUDWATCH_ALARM_PRICE
    comp = CostComponent(
        name="Alarm",
        unit="months",
        hourly_quantity=None,
        monthly_quantity=1.0,
        price=price,
        hourly_cost=price / 730,
        monthly_cost=price,
        usage_based=False,
    )
    return Resource(
        name=tf.address,
        resource_type=tf.type,
        tags=tf.values.get("tags") or {},
        monthly_cost=price,
        hourly_cost=price / 730,
        cost_components=[comp],
        sub_resources=[],
    )


def _price_cloudwatch_log_group(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """
    aws_cloudwatch_log_group: fully usage-based, same as S3/SQS — a log
    group's ingestion volume and retained size aren't knowable from its
    config (log volume is driven by what the application writes, and
    `retention_in_days` bounds *how long* stored bytes are billed, not
    *how many* bytes there are). Two independent usage-based components,
    same split AWS bills: bytes ingested and bytes stored.
    """
    components = []
    ingestion_row = price_db.get_price("AmazonCloudWatch", region, "cloudwatch:logs:ingestion", db=db)
    if ingestion_row is not None:
        components.append(CostComponent(
            name="Data ingested",
            unit="GB",
            hourly_quantity=None,
            monthly_quantity=None,
            price=ingestion_row["price_usd"],
            hourly_cost=None,
            monthly_cost=None,
            usage_based=True,
        ))
    storage_row = price_db.get_price("AmazonCloudWatch", region, "cloudwatch:logs:storage", db=db)
    if storage_row is not None:
        components.append(CostComponent(
            name="Data stored",
            unit="GB-months",
            hourly_quantity=None,
            monthly_quantity=None,
            price=storage_row["price_usd"],
            hourly_cost=None,
            monthly_cost=None,
            usage_based=True,
        ))
    if not components:
        return _unpriced(tf, f"no CloudWatch Logs price data in {region}")
    return Resource(
        name=tf.address,
        resource_type=tf.type,
        tags=tf.values.get("tags") or {},
        monthly_cost=None,
        hourly_cost=None,
        cost_components=components,
        sub_resources=[],
    )


def _price_sqs_queue(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    queue_type = "fifo" if tf.values.get("fifo_queue") else "standard"
    row = price_db.get_price("AWSQueueService", region, f"sqs:requests:{queue_type}", db=db)
    if row is None:
        return _unpriced(tf, f"no SQS {queue_type} price data in {region}")
    comp = CostComponent(
        name=f"Requests ({queue_type})",
        unit="1M requests",
        hourly_quantity=None,
        monthly_quantity=None,
        price=row["price_usd"] * _PER_MILLION,
        hourly_cost=None,
        monthly_cost=None,
        usage_based=True,
    )
    return Resource(
        name=tf.address,
        resource_type=tf.type,
        tags=tf.values.get("tags") or {},
        monthly_cost=None,
        hourly_cost=None,
        cost_components=[comp],
        sub_resources=[],
    )


def _price_secretsmanager_secret(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """
    A secret's per-month price is fixed regardless of its config, so unlike S3/SQS/
    Lambda this resource always has a known monthly_cost. API request volume isn't,
    so that component stays usage-based alongside it.
    """
    row = price_db.get_price("AWSSecretsManager", region, "secretsmanager:secret", db=db)
    if row is None:
        return _unpriced(tf, f"no Secrets Manager price data in {region}")
    price = row["price_usd"]
    fixed_comp = CostComponent(
        name="Secret",
        unit="months",
        hourly_quantity=None,
        monthly_quantity=1.0,
        price=price,
        hourly_cost=price / 730,
        monthly_cost=price,
        usage_based=False,
    )
    req_row = price_db.get_price("AWSSecretsManager", region, "secretsmanager:requests", db=db)
    requests_comp = CostComponent(
        name="API requests",
        unit="1M requests",
        hourly_quantity=None,
        monthly_quantity=None,
        price=req_row["price_usd"] * _PER_MILLION if req_row else None,
        hourly_cost=None,
        monthly_cost=None,
        usage_based=True,
    )
    return Resource(
        name=tf.address,
        resource_type=tf.type,
        tags=tf.values.get("tags") or {},
        monthly_cost=price,
        hourly_cost=price / 730,
        cost_components=[fixed_comp, requests_comp],
        sub_resources=[],
    )


def _price_route53_zone(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """
    A hosted zone's base price is fixed, like a Secrets Manager secret's — known
    from config alone. Query volume isn't, so that component stays usage-based.
    Uses the first-tier zone/query rate; see fetch_route53 for why.
    """
    row = price_db.get_price("AmazonRoute53", region, "route53:hostedzone", db=db)
    if row is None:
        return _unpriced(tf, f"no Route 53 price data in {region}")
    price = row["price_usd"]
    fixed_comp = CostComponent(
        name="Hosted zone", unit="months",
        hourly_quantity=None, monthly_quantity=1.0,
        price=price, hourly_cost=price / 730, monthly_cost=price, usage_based=False,
    )
    query_row = price_db.get_price("AmazonRoute53", region, "route53:queries", db=db)
    query_comp = CostComponent(
        name="Standard queries", unit="1M queries",
        hourly_quantity=None, monthly_quantity=None,
        price=query_row["price_usd"] * _PER_MILLION if query_row else None,
        hourly_cost=None, monthly_cost=None, usage_based=True,
    )
    return Resource(
        name=tf.address, resource_type=tf.type, tags=tf.values.get("tags") or {},
        monthly_cost=price, hourly_cost=price / 730,
        cost_components=[fixed_comp, query_comp], sub_resources=[],
    )


def _price_kms_key(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """Like Route 53/Secrets Manager: the per-key price is fixed; request volume isn't."""
    row = price_db.get_price("awskms", region, "kms:key", db=db)
    if row is None:
        return _unpriced(tf, f"no KMS price data in {region}")
    price = row["price_usd"]
    fixed_comp = CostComponent(
        name="Customer managed key", unit="months",
        hourly_quantity=None, monthly_quantity=1.0,
        price=price, hourly_cost=price / 730, monthly_cost=price, usage_based=False,
    )
    req_row = price_db.get_price("awskms", region, "kms:requests", db=db)
    requests_comp = CostComponent(
        name="API requests (symmetric)", unit="1M requests",
        hourly_quantity=None, monthly_quantity=None,
        price=req_row["price_usd"] * _PER_MILLION if req_row else None,
        hourly_cost=None, monthly_cost=None, usage_based=True,
    )
    return Resource(
        name=tf.address, resource_type=tf.type, tags=tf.values.get("tags") or {},
        monthly_cost=price, hourly_cost=price / 730,
        cost_components=[fixed_comp, requests_comp], sub_resources=[],
    )


def _price_waf_web_acl(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """
    Web ACL and rule count are both fixed, known-from-config charges (rule
    count comes straight from the `rule` blocks); requests are usage-based and
    priced at WAF's flat baseline rate (see fetch_waf's WCU-tier caveat).
    """
    values = tf.values
    webacl_row = price_db.get_price("awswaf", region, "waf:webacl", db=db)
    if webacl_row is None:
        return _unpriced(tf, f"no WAF price data in {region}")
    webacl_price = webacl_row["price_usd"]
    comps = [CostComponent(
        name="Web ACL", unit="months",
        hourly_quantity=None, monthly_quantity=1.0,
        price=webacl_price, hourly_cost=webacl_price / 730,
        monthly_cost=webacl_price, usage_based=False,
    )]
    monthly_cost = webacl_price

    rules = values.get("rule") or []
    if isinstance(rules, dict):
        rules = [rules]
    if rules:
        rule_row = price_db.get_price("awswaf", region, "waf:rule", db=db)
        if rule_row is not None:
            rule_price = rule_row["price_usd"]
            rules_cost = rule_price * len(rules)
            comps.append(CostComponent(
                name=f"Rules ({len(rules)})", unit="rule-months",
                hourly_quantity=None, monthly_quantity=float(len(rules)),
                price=rule_price, hourly_cost=None,
                monthly_cost=rules_cost, usage_based=False,
            ))
            monthly_cost += rules_cost

    req_row = price_db.get_price("awswaf", region, "waf:requests", db=db)
    comps.append(CostComponent(
        name="Requests", unit="1M requests",
        hourly_quantity=None, monthly_quantity=None,
        price=req_row["price_usd"] * _PER_MILLION if req_row else None,
        hourly_cost=None, monthly_cost=None, usage_based=True,
    ))

    return Resource(
        name=tf.address, resource_type=tf.type, tags=values.get("tags") or {},
        monthly_cost=monthly_cost, hourly_cost=monthly_cost / 730,
        cost_components=comps, sub_resources=[],
    )


def _price_eks_cluster(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """
    Flat control-plane hourly rate, known regardless of config — no
    config-dependent variant to select (see fetch_eks). Worker capacity
    (`aws_eks_node_group`, `aws_instance`, Fargate profiles) is priced
    separately by the existing EC2/Fargate pricers.
    """
    row = price_db.get_price("AmazonEKS", region, "eks:cluster", db=db)
    if row is None:
        return _unpriced(tf, f"no EKS price data in {region}")
    price = row["price_usd"]
    comp = CostComponent(
        name="EKS cluster", unit="hours",
        hourly_quantity=1.0, monthly_quantity=730.0,
        price=price, hourly_cost=price, monthly_cost=price * 730, usage_based=False,
    )
    return Resource(
        name=tf.address, resource_type=tf.type, tags=tf.values.get("tags") or {},
        monthly_cost=price * 730, hourly_cost=price,
        cost_components=[comp], sub_resources=[],
    )


def _price_dynamodb_table(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """
    Storage is always usage-based (table size isn't in config). Provisioned
    tables (`billing_mode = "PROVISIONED"`, terraform's default) also get a
    known monthly cost from `read_capacity`/`write_capacity`, same shape as
    EC2 instance-hours; on-demand tables (`PAY_PER_REQUEST`) leave request
    volume as usage-based components instead, like S3/SQS.
    """
    values = tf.values
    storage_row = price_db.get_price("AmazonDynamoDB", region, "dynamodb:storage", db=db)
    if storage_row is None:
        return _unpriced(tf, f"no DynamoDB price data in {region}")
    comps = [CostComponent(
        name="Storage", unit="GB-months",
        hourly_quantity=None, monthly_quantity=None,
        price=storage_row["price_usd"], hourly_cost=None, monthly_cost=None, usage_based=True,
    )]

    billing_mode = (values.get("billing_mode") or "PROVISIONED").upper()
    monthly_cost = None
    if billing_mode == "PROVISIONED":
        read_row = price_db.get_price("AmazonDynamoDB", region, "dynamodb:provisioned:read", db=db)
        write_row = price_db.get_price("AmazonDynamoDB", region, "dynamodb:provisioned:write", db=db)
        read_units = float(values.get("read_capacity") or 0)
        write_units = float(values.get("write_capacity") or 0)
        if read_row is not None:
            read_cost = read_units * read_row["price_usd"] * 730
            comps.append(CostComponent(
                name="Provisioned read capacity", unit="RCU-hours",
                hourly_quantity=read_units, monthly_quantity=read_units * 730,
                price=read_row["price_usd"], hourly_cost=read_units * read_row["price_usd"],
                monthly_cost=read_cost, usage_based=False,
            ))
            monthly_cost = (monthly_cost or 0.0) + read_cost
        if write_row is not None:
            write_cost = write_units * write_row["price_usd"] * 730
            comps.append(CostComponent(
                name="Provisioned write capacity", unit="WCU-hours",
                hourly_quantity=write_units, monthly_quantity=write_units * 730,
                price=write_row["price_usd"], hourly_cost=write_units * write_row["price_usd"],
                monthly_cost=write_cost, usage_based=False,
            ))
            monthly_cost = (monthly_cost or 0.0) + write_cost
    else:
        read_req_row = price_db.get_price("AmazonDynamoDB", region, "dynamodb:ondemand:read", db=db)
        write_req_row = price_db.get_price("AmazonDynamoDB", region, "dynamodb:ondemand:write", db=db)
        comps.append(CostComponent(
            name="On-demand read requests", unit="1M requests",
            hourly_quantity=None, monthly_quantity=None,
            price=read_req_row["price_usd"] * _PER_MILLION if read_req_row else None,
            hourly_cost=None, monthly_cost=None, usage_based=True,
        ))
        comps.append(CostComponent(
            name="On-demand write requests", unit="1M requests",
            hourly_quantity=None, monthly_quantity=None,
            price=write_req_row["price_usd"] * _PER_MILLION if write_req_row else None,
            hourly_cost=None, monthly_cost=None, usage_based=True,
        ))

    return Resource(
        name=tf.address, resource_type=tf.type, tags=values.get("tags") or {},
        monthly_cost=monthly_cost, hourly_cost=(monthly_cost / 730) if monthly_cost is not None else None,
        cost_components=comps, sub_resources=[],
    )


def price_data_transfer(region: str, db=None) -> Optional[Resource]:
    """
    Synthetic "Data Transfer" resource — not tied to any single Terraform
    resource, so it doesn't go through `_PRICERS`/`price_resources`. Callers
    append it to a breakdown directly (see `price-state` in cli.py), after
    diffing, since it has no fixed cost and would otherwise show up as
    spuriously "added" on every plan diff.

    Every component is unit-priced only, with no quantity or total: internet
    egress is billed against an account-wide cumulative tier that a single
    Terraform plan can't resolve, and inter-AZ volume isn't in Terraform
    config at all. Real numbers need either a usage file or Cost Explorer/CUR
    actuals (not implemented yet) to fill in a quantity.
    """
    rows = price_db.get_all("AWSDataTransfer", region, db=db)
    tier_rows = sorted(
        (r for r in rows if r["price_key"].startswith("datatransfer:out:")),
        key=lambda r: int(r["price_key"].rsplit(":", 1)[1]),
    )
    regional_row = next((r for r in rows if r["price_key"] == "datatransfer:regional"), None)
    if not tier_rows and regional_row is None:
        return None

    comps = []
    for row in tier_rows:
        begin_gb = int(row["price_key"].rsplit(":", 1)[1])
        name = "Internet egress, first tier" if begin_gb == 0 else f"Internet egress, above {begin_gb:,} GB/mo"
        comps.append(CostComponent(
            name=name, unit="GB",
            hourly_quantity=None, monthly_quantity=None,
            price=row["price_usd"], hourly_cost=None, monthly_cost=None, usage_based=True,
        ))
    if regional_row is not None:
        comps.append(CostComponent(
            name="Inter-AZ transfer", unit="GB",
            hourly_quantity=None, monthly_quantity=None,
            price=regional_row["price_usd"], hourly_cost=None, monthly_cost=None, usage_based=True,
        ))

    return Resource(
        name=f"Data Transfer ({region})", resource_type="aws_data_transfer",
        tags={}, monthly_cost=None, hourly_cost=None,
        cost_components=comps, sub_resources=[],
    )


def estimate_data_transfer_cost(region: str, usage: dict, db=None) -> Optional[float]:
    """
    Turn `price_data_transfer`'s unit-priced components into a real monthly
    total using user-supplied quantities from a usage file (see
    usage_file.py) — the same "fill in what you can" idea as
    estimator.estimate_resource_cost, just fed by a static number instead of
    a live CloudWatch metric (CUR actuals would slot in the same way later).

    Internet egress is split across every cumulative tier the way AWS
    actually bills it: the first N GB at tier 0's rate, the next chunk at
    tier 1's, and so on. Returns None if the usage file has nothing relevant
    or no tier prices are cached for `region`.
    """
    from .usage_file import data_transfer_usage

    egress_gb, inter_az_gb = data_transfer_usage(usage)
    if egress_gb is None and inter_az_gb is None:
        return None

    rows = price_db.get_all("AWSDataTransfer", region, db=db)
    tier_rows = sorted(
        (r for r in rows if r["price_key"].startswith("datatransfer:out:")),
        key=lambda r: int(r["price_key"].rsplit(":", 1)[1]),
    )
    regional_row = next((r for r in rows if r["price_key"] == "datatransfer:regional"), None)

    total = 0.0
    computed_any = False

    if egress_gb is not None and tier_rows:
        begins = [int(r["price_key"].rsplit(":", 1)[1]) for r in tier_rows]
        for i, row in enumerate(tier_rows):
            begin = begins[i]
            end = begins[i + 1] if i + 1 < len(begins) else None
            qty = (min(egress_gb, end) if end is not None else egress_gb) - begin
            total += max(qty, 0.0) * row["price_usd"]
        computed_any = True

    if inter_az_gb is not None and regional_row is not None:
        total += inter_az_gb * regional_row["price_usd"]
        computed_any = True

    return round(total, 6) if computed_any else None


def _price_vpc_endpoint(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """
    Gateway endpoints (S3/DynamoDB, terraform's default `vpc_endpoint_type`)
    are free — no Pricing API line item exists for them, so this returns a
    flat $0 resource rather than `_unpriced` (it's a known cost, not a
    missing price). Interface endpoints bill a flat hourly rate per AZ
    (one charge per subnet the endpoint's ENI lands in, from `subnet_ids`)
    plus usage-based per-GB data processed, same hourly-plus-usage shape as
    NAT Gateway.
    """
    values = tf.values
    endpoint_type = (values.get("vpc_endpoint_type") or "Gateway").capitalize()
    if endpoint_type != "Interface":
        comp = CostComponent(
            name="Gateway endpoint", unit="months",
            hourly_quantity=None, monthly_quantity=None,
            price=0.0, hourly_cost=0.0, monthly_cost=0.0, usage_based=False,
        )
        return Resource(
            name=tf.address, resource_type=tf.type, tags=values.get("tags") or {},
            monthly_cost=0.0, hourly_cost=0.0,
            cost_components=[comp], sub_resources=[],
        )

    hourly_row = price_db.get_price("AmazonVPC", region, "vpcendpoint:hourly", db=db)
    if hourly_row is None:
        return _unpriced(tf, f"no VPC endpoint price data in {region}")
    az_count = len(values.get("subnet_ids") or []) or 1
    rate = hourly_row["price_usd"]
    monthly_cost = rate * az_count * 730
    fixed_comp = CostComponent(
        name=f"Interface endpoint ({az_count} AZ{'s' if az_count != 1 else ''})", unit="hours",
        hourly_quantity=float(az_count), monthly_quantity=float(az_count) * 730,
        price=rate, hourly_cost=rate * az_count, monthly_cost=monthly_cost, usage_based=False,
    )
    data_row = price_db.get_price("AmazonVPC", region, "vpcendpoint:data", db=db)
    data_comp = CostComponent(
        name="Data processed", unit="GB",
        hourly_quantity=None, monthly_quantity=None,
        price=data_row["price_usd"] if data_row else None,
        hourly_cost=None, monthly_cost=None, usage_based=True,
    )
    return Resource(
        name=tf.address, resource_type=tf.type, tags=values.get("tags") or {},
        monthly_cost=monthly_cost, hourly_cost=rate * az_count,
        cost_components=[fixed_comp, data_comp], sub_resources=[],
    )


def _price_sns_topic(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """Entirely usage-based, like SQS: a topic has no fixed monthly cost,
    only per-publish/API-request pricing driven by traffic the config can't
    reveal."""
    row = price_db.get_price("AmazonSNS", region, "sns:requests", db=db)
    if row is None:
        return _unpriced(tf, f"no SNS price data in {region}")
    comp = CostComponent(
        name="Requests", unit="1M requests",
        hourly_quantity=None, monthly_quantity=None,
        price=row["price_usd"] * _PER_MILLION, hourly_cost=None, monthly_cost=None, usage_based=True,
    )
    return Resource(
        name=tf.address, resource_type=tf.type, tags=tf.values.get("tags") or {},
        monthly_cost=None, hourly_cost=None,
        cost_components=[comp], sub_resources=[],
    )


def _price_efs_file_system(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """Entirely usage-based, like S3: file system size isn't in config."""
    row = price_db.get_price("AmazonEFS", region, "efs:storage:standard", db=db)
    if row is None:
        return _unpriced(tf, f"no EFS price data in {region}")
    comp = CostComponent(
        name="Standard storage", unit="GB-months",
        hourly_quantity=None, monthly_quantity=None,
        price=row["price_usd"], hourly_cost=None, monthly_cost=None, usage_based=True,
    )
    return Resource(
        name=tf.address, resource_type=tf.type, tags=tf.values.get("tags") or {},
        monthly_cost=None, hourly_cost=None,
        cost_components=[comp], sub_resources=[],
    )


def _price_ecr_repository(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """Entirely usage-based, like S3/EFS: image storage size isn't in config."""
    row = price_db.get_price("AmazonECR", region, "ecr:storage", db=db)
    if row is None:
        return _unpriced(tf, f"no ECR price data in {region}")
    comp = CostComponent(
        name="Storage", unit="GB-months",
        hourly_quantity=None, monthly_quantity=None,
        price=row["price_usd"], hourly_cost=None, monthly_cost=None, usage_based=True,
    )
    return Resource(
        name=tf.address, resource_type=tf.type, tags=tf.values.get("tags") or {},
        monthly_cost=None, hourly_cost=None,
        cost_components=[comp], sub_resources=[],
    )


def _price_api_gateway_rest_api(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """Entirely usage-based, like SNS/SQS: request volume isn't in config."""
    row = price_db.get_price("AmazonApiGateway", region, "apigateway:rest:requests", db=db)
    if row is None:
        return _unpriced(tf, f"no API Gateway REST price data in {region}")
    comp = CostComponent(
        name="Requests", unit="1M requests",
        hourly_quantity=None, monthly_quantity=None,
        price=row["price_usd"] * _PER_MILLION, hourly_cost=None, monthly_cost=None, usage_based=True,
    )
    return Resource(
        name=tf.address, resource_type=tf.type, tags=tf.values.get("tags") or {},
        monthly_cost=None, hourly_cost=None,
        cost_components=[comp], sub_resources=[],
    )


def _price_api_gateway_v2_api(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """
    HTTP APIs are entirely usage-based, same shape as REST APIs. WebSocket
    APIs bill per-message and per-connection-minute instead of per-request —
    prices for that aren't fetched (see fetch_apigateway), so they come back
    unsupported rather than silently priced at the wrong (HTTP) rate.
    """
    protocol = (tf.values.get("protocol_type") or "HTTP").upper()
    if protocol != "HTTP":
        return _unpriced(tf, f"unsupported API Gateway v2 protocol '{protocol}'")
    row = price_db.get_price("AmazonApiGateway", region, "apigateway:http:requests", db=db)
    if row is None:
        return _unpriced(tf, f"no API Gateway HTTP price data in {region}")
    comp = CostComponent(
        name="Requests", unit="1M requests",
        hourly_quantity=None, monthly_quantity=None,
        price=row["price_usd"] * _PER_MILLION, hourly_cost=None, monthly_cost=None, usage_based=True,
    )
    return Resource(
        name=tf.address, resource_type=tf.type, tags=tf.values.get("tags") or {},
        monthly_cost=None, hourly_cost=None,
        cost_components=[comp], sub_resources=[],
    )


def _price_cloudfront_distribution(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """
    Entirely usage-based, like S3/SNS: neither data-transfer volume nor
    request count is in a distribution's config. Priced against the
    US/Canada/Europe edge-location group's first tier only — see
    `fetch_cloudfront`'s docstring for the geography simplification this
    implies for distributions serving mostly other regions.
    """
    data_row = price_db.get_price("AmazonCloudFront", region, "cloudfront:data:out", db=db)
    requests_row = price_db.get_price("AmazonCloudFront", region, "cloudfront:requests:https", db=db)
    if data_row is None and requests_row is None:
        return _unpriced(tf, f"no CloudFront price data in {region}")
    comps = [
        CostComponent(
            name="Data transfer out (US/Canada/Europe)", unit="GB",
            hourly_quantity=None, monthly_quantity=None,
            price=data_row["price_usd"] if data_row else None,
            hourly_cost=None, monthly_cost=None, usage_based=True,
        ),
        CostComponent(
            name="HTTPS requests", unit="1M requests",
            hourly_quantity=None, monthly_quantity=None,
            price=requests_row["price_usd"] * _PER_MILLION if requests_row else None,
            hourly_cost=None, monthly_cost=None, usage_based=True,
        ),
    ]
    return Resource(
        name=tf.address, resource_type=tf.type, tags=tf.values.get("tags") or {},
        monthly_cost=None, hourly_cost=None,
        cost_components=comps, sub_resources=[],
    )


def _price_kinesis_stream(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """
    Provisioned mode (terraform's default when `stream_mode_details` is
    omitted) gets a known monthly cost from `shard_count` — same shape as
    EC2 instance-hours. On-demand mode bills per-GB, not per-shard, and
    isn't fetched (see fetch_kinesis), so it comes back unsupported.
    """
    values = tf.values
    mode_details = values.get("stream_mode_details")
    if isinstance(mode_details, list):
        mode_details = mode_details[0] if mode_details else {}
    stream_mode = ((mode_details or {}).get("stream_mode") or "PROVISIONED").upper()
    if stream_mode != "PROVISIONED":
        return _unpriced(tf, f"unsupported Kinesis stream mode '{stream_mode}'")

    shard_row = price_db.get_price("AmazonKinesis", region, "kinesis:shard:hour", db=db)
    if shard_row is None:
        return _unpriced(tf, f"no Kinesis price data in {region}")
    shard_count = int(values.get("shard_count") or 1)
    rate = shard_row["price_usd"]
    monthly_cost = shard_count * rate * 730
    fixed_comp = CostComponent(
        name=f"Shard hours ({shard_count} shard{'s' if shard_count != 1 else ''})", unit="hours",
        hourly_quantity=float(shard_count), monthly_quantity=float(shard_count) * 730,
        price=rate, hourly_cost=shard_count * rate, monthly_cost=monthly_cost, usage_based=False,
    )
    payload_row = price_db.get_price("AmazonKinesis", region, "kinesis:payload:units", db=db)
    payload_comp = CostComponent(
        name="PUT payload units", unit="1M units",
        hourly_quantity=None, monthly_quantity=None,
        price=payload_row["price_usd"] * _PER_MILLION if payload_row else None,
        hourly_cost=None, monthly_cost=None, usage_based=True,
    )
    return Resource(
        name=tf.address, resource_type=tf.type, tags=values.get("tags") or {},
        monthly_cost=monthly_cost, hourly_cost=shard_count * rate,
        cost_components=[fixed_comp, payload_comp], sub_resources=[],
    )


def _price_sfn_state_machine(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """
    Entirely usage-based regardless of workflow type: Standard workflows
    bill per state transition, Express workflows bill per request plus
    GB-seconds of duration — none of which is derivable from
    `aws_sfn_state_machine`'s config beyond which of the two `type` is.
    """
    sfn_type = (tf.values.get("type") or "STANDARD").upper()
    if sfn_type == "EXPRESS":
        req_row = price_db.get_price("AmazonStates", region, "sfn:express:requests", db=db)
        dur_row = price_db.get_price("AmazonStates", region, "sfn:express:duration", db=db)
        if req_row is None and dur_row is None:
            return _unpriced(tf, f"no Step Functions Express price data in {region}")
        comps = [
            CostComponent(
                name="Requests", unit="1M requests",
                hourly_quantity=None, monthly_quantity=None,
                price=req_row["price_usd"] * _PER_MILLION if req_row else None,
                hourly_cost=None, monthly_cost=None, usage_based=True,
            ),
            CostComponent(
                name="Duration", unit="GB-seconds",
                hourly_quantity=None, monthly_quantity=None,
                price=dur_row["price_usd"] if dur_row else None,
                hourly_cost=None, monthly_cost=None, usage_based=True,
            ),
        ]
    else:
        row = price_db.get_price("AmazonStates", region, "sfn:standard:transitions", db=db)
        if row is None:
            return _unpriced(tf, f"no Step Functions Standard price data in {region}")
        comps = [CostComponent(
            name="State transitions", unit="1K transitions",
            hourly_quantity=None, monthly_quantity=None,
            price=row["price_usd"] * 1_000, hourly_cost=None, monthly_cost=None, usage_based=True,
        )]
    return Resource(
        name=tf.address, resource_type=tf.type, tags=tf.values.get("tags") or {},
        monthly_cost=None, hourly_cost=None,
        cost_components=comps, sub_resources=[],
    )


def _price_eventbridge_event_bus(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """Entirely usage-based, like SNS: a custom bus has no fixed monthly
    cost, only per-published-event pricing driven by traffic the config
    can't reveal."""
    row = price_db.get_price("AWSEvents", region, "eventbridge:events", db=db)
    if row is None:
        return _unpriced(tf, f"no EventBridge price data in {region}")
    comp = CostComponent(
        name="Custom events published", unit="1M events",
        hourly_quantity=None, monthly_quantity=None,
        price=row["price_usd"] * _PER_MILLION, hourly_cost=None, monthly_cost=None, usage_based=True,
    )
    return Resource(
        name=tf.address, resource_type=tf.type, tags=tf.values.get("tags") or {},
        monthly_cost=None, hourly_cost=None,
        cost_components=[comp], sub_resources=[],
    )


def _price_transit_gateway_attachment(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """Flat per-attachment hourly rate (always exactly one attachment per
    resource) plus usage-based per-GB data processed, same hourly-plus-usage
    shape as NAT Gateway/VPC Interface Endpoints."""
    row = price_db.get_price("AmazonVPC", region, "transitgateway:hourly", db=db)
    if row is None:
        return _unpriced(tf, f"no Transit Gateway price data in {region}")
    rate = row["price_usd"]
    monthly_cost = rate * 730
    fixed_comp = CostComponent(
        name="Attachment", unit="hours",
        hourly_quantity=1.0, monthly_quantity=730.0,
        price=rate, hourly_cost=rate, monthly_cost=monthly_cost, usage_based=False,
    )
    data_row = price_db.get_price("AmazonVPC", region, "transitgateway:data", db=db)
    data_comp = CostComponent(
        name="Data processed", unit="GB",
        hourly_quantity=None, monthly_quantity=None,
        price=data_row["price_usd"] if data_row else None,
        hourly_cost=None, monthly_cost=None, usage_based=True,
    )
    return Resource(
        name=tf.address, resource_type=tf.type, tags=tf.values.get("tags") or {},
        monthly_cost=monthly_cost, hourly_cost=rate,
        cost_components=[fixed_comp, data_comp], sub_resources=[],
    )


def _price_s3files_file_system(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """
    Entirely usage-based, like S3/EFS: how much data lands in the
    high-performance storage tier (a rolling window of recently-touched
    data, sized by the file system's `cache_expiration_days`/file-size
    tunables, not a config-derivable byte count) and how much moves onto
    and off of it, isn't in the file system's own config. The underlying
    bucket's ordinary S3 storage cost is priced separately by
    `_price_s3_bucket` against the `aws_s3_bucket` resource itself — not
    duplicated here. Unlike ordinary S3 requests, write/read rates here are
    per-GB, not per-request — no `_PER_MILLION` scaling.
    """
    storage_row = price_db.get_price("AmazonS3", region, "s3files:storage", db=db)
    if storage_row is None:
        return _unpriced(tf, f"no S3 Files price data in {region}")
    write_row = price_db.get_price("AmazonS3", region, "s3files:write", db=db)
    read_row = price_db.get_price("AmazonS3", region, "s3files:read", db=db)
    comps = [
        CostComponent(
            name="High-performance storage", unit="GB-months",
            hourly_quantity=None, monthly_quantity=None,
            price=storage_row["price_usd"], hourly_cost=None, monthly_cost=None, usage_based=True,
        ),
        CostComponent(
            name="Data written to fast tier", unit="GB",
            hourly_quantity=None, monthly_quantity=None,
            price=write_row["price_usd"] if write_row else None,
            hourly_cost=None, monthly_cost=None, usage_based=True,
        ),
        CostComponent(
            name="Data read from fast tier", unit="GB",
            hourly_quantity=None, monthly_quantity=None,
            price=read_row["price_usd"] if read_row else None,
            hourly_cost=None, monthly_cost=None, usage_based=True,
        ),
    ]
    return Resource(
        name=tf.address, resource_type=tf.type, tags=tf.values.get("tags") or {},
        monthly_cost=None, hourly_cost=None,
        cost_components=comps, sub_resources=[],
    )


def _price_s3files_free_resource(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """Mount targets and access points carry no charge of their own — all
    cost is on the file system's cache storage/requests."""
    label = "Mount target" if tf.type == "aws_s3files_mount_target" else "Access point"
    comp = CostComponent(
        name=label, unit="months",
        hourly_quantity=None, monthly_quantity=None,
        price=0.0, hourly_cost=0.0, monthly_cost=0.0, usage_based=False,
    )
    return Resource(
        name=tf.address, resource_type=tf.type, tags=tf.values.get("tags") or {},
        monthly_cost=0.0, hourly_cost=0.0,
        cost_components=[comp], sub_resources=[],
    )


def _price_opensearch_domain(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """
    Data-node instance-hours (known from `cluster_config.instance_type` /
    `instance_count`) plus EBS storage (known from `ebs_options.volume_size`
    / `volume_type`) — both fully config-derivable, same shape as RDS +
    attached EBS. Dedicated master nodes (`dedicated_master_enabled`) and
    UltraWarm/cold-storage nodes (`warm_enabled`) aren't priced — their
    instance-hour cost is real but omitted here, so a domain using them will
    under-report its total.
    """
    values = tf.values
    cluster = values.get("cluster_config")
    if isinstance(cluster, list):
        cluster = cluster[0] if cluster else {}
    cluster = cluster or {}
    instance_type = cluster.get("instance_type")
    if not instance_type:
        return _unpriced(tf, "missing cluster_config.instance_type")
    row = price_db.get_price("AmazonES", region, f"opensearch:{instance_type}", db=db)
    if row is None:
        return _unpriced(tf, f"no OpenSearch price data for {instance_type} in {region}")
    instance_count = int(cluster.get("instance_count") or 1)
    rate = row["price_usd"]
    monthly_cost = instance_count * rate * 730
    comps = [CostComponent(
        name=f"Data nodes ({instance_count}x {instance_type})", unit="hours",
        hourly_quantity=float(instance_count), monthly_quantity=float(instance_count) * 730,
        price=rate, hourly_cost=instance_count * rate, monthly_cost=monthly_cost, usage_based=False,
    )]

    ebs = values.get("ebs_options")
    if isinstance(ebs, list):
        ebs = ebs[0] if ebs else {}
    ebs = ebs or {}
    volume_size = ebs.get("volume_size")
    if ebs.get("ebs_enabled", True) and volume_size:
        volume_type = (ebs.get("volume_type") or "gp2").lower()
        storage_row = price_db.get_price("AmazonES", region, f"opensearch:storage:{volume_type}", db=db)
        if storage_row is not None:
            storage_cost = float(volume_size) * storage_row["price_usd"]
            comps.append(CostComponent(
                name=f"EBS storage ({volume_type}, {volume_size} GB)", unit="GB-months",
                hourly_quantity=None, monthly_quantity=float(volume_size),
                price=storage_row["price_usd"], hourly_cost=None, monthly_cost=storage_cost, usage_based=False,
            ))
            monthly_cost += storage_cost

    return Resource(
        name=tf.address, resource_type=tf.type, tags=values.get("tags") or {},
        monthly_cost=monthly_cost, hourly_cost=monthly_cost / 730,
        cost_components=comps, sub_resources=[],
    )


def _price_redshift_cluster(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """
    Node-hours (config-derivable from `node_type`/`number_of_nodes`) plus,
    for RA3 node types only, managed-storage cost — RA3 bills storage
    separately from compute; DC2/DS2 node types have storage baked into the
    node-hour rate, so no separate storage component applies to them.
    Storage usage itself isn't config-derivable, so when it does apply it's
    left usage-based (unit price only, no total), same pattern as S3/EFS.
    """
    values = tf.values
    node_type = values.get("node_type")
    if not node_type:
        return _unpriced(tf, "missing node_type")
    row = price_db.get_price("AmazonRedshift", region, f"redshift:{node_type}", db=db)
    if row is None:
        return _unpriced(tf, f"no Redshift price data for {node_type} in {region}")
    node_count = int(values.get("number_of_nodes") or 1)
    rate = row["price_usd"]
    monthly_cost = node_count * rate * 730
    comps = [CostComponent(
        name=f"Compute nodes ({node_count}x {node_type})", unit="hours",
        hourly_quantity=float(node_count), monthly_quantity=float(node_count) * 730,
        price=rate, hourly_cost=node_count * rate, monthly_cost=monthly_cost, usage_based=False,
    )]

    if node_type.startswith("ra3."):
        storage_row = price_db.get_price("AmazonRedshift", region, "redshift:storage", db=db)
        if storage_row is not None:
            comps.append(CostComponent(
                name="Managed storage", unit="GB-months",
                hourly_quantity=None, monthly_quantity=None,
                price=storage_row["price_usd"], hourly_cost=None, monthly_cost=None, usage_based=True,
            ))

    return Resource(
        name=tf.address, resource_type=tf.type, tags=values.get("tags") or {},
        monthly_cost=monthly_cost, hourly_cost=monthly_cost / 730,
        cost_components=comps, sub_resources=[],
    )


def _price_backup_vault(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """
    Fully usage-based like S3/Config: how much backed-up data a vault ends
    up holding (warm vs. cold storage tier) and how much gets restored
    isn't in the vault's own config — it depends on what backup plans and
    jobs write into it over time.
    """
    warm_row = price_db.get_price("AWSBackup", region, "backup:storage:warm", db=db)
    if warm_row is None:
        return _unpriced(tf, f"no AWS Backup price data in {region}")
    cold_row = price_db.get_price("AWSBackup", region, "backup:storage:cold", db=db)
    restore_row = price_db.get_price("AWSBackup", region, "backup:restore", db=db)
    comps = [
        CostComponent(
            name="Warm storage", unit="GB-months",
            hourly_quantity=None, monthly_quantity=None,
            price=warm_row["price_usd"], hourly_cost=None, monthly_cost=None, usage_based=True,
        ),
    ]
    if cold_row is not None:
        comps.append(CostComponent(
            name="Cold storage", unit="GB-months",
            hourly_quantity=None, monthly_quantity=None,
            price=cold_row["price_usd"], hourly_cost=None, monthly_cost=None, usage_based=True,
        ))
    if restore_row is not None:
        comps.append(CostComponent(
            name="Restore", unit="GB",
            hourly_quantity=None, monthly_quantity=None,
            price=restore_row["price_usd"], hourly_cost=None, monthly_cost=None, usage_based=True,
        ))
    return Resource(
        name=tf.address, resource_type=tf.type, tags=tf.values.get("tags") or {},
        monthly_cost=None, hourly_cost=None,
        cost_components=comps, sub_resources=[],
    )


def _price_backup_plan_free(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """A backup plan is scheduling/policy config, not its own billable
    resource — the storage and restore charges it drives land on the vault
    (`_price_backup_vault`), so this prices as a known $0."""
    comp = CostComponent(
        name="Backup plan", unit="months",
        hourly_quantity=None, monthly_quantity=None,
        price=0.0, hourly_cost=0.0, monthly_cost=0.0, usage_based=False,
    )
    return Resource(
        name=tf.address, resource_type=tf.type, tags=tf.values.get("tags") or {},
        monthly_cost=0.0, hourly_cost=0.0,
        cost_components=[comp], sub_resources=[],
    )


def _price_msk_cluster(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """
    Broker instance-hours plus EBS broker storage — both config-derivable
    from `broker_node_group_info` (`instance_type`, `number_of_broker_nodes`,
    `storage_info.ebs_storage_info.volume_size`), same shape as
    RDS+attached-EBS. `storage_info` may be omitted entirely by older
    provider versions (a default volume applies) — when it's missing, no
    storage component is added since the default size isn't in this
    resource's config either.
    """
    values = tf.values
    broker_info = values.get("broker_node_group_info")
    if isinstance(broker_info, list):
        broker_info = broker_info[0] if broker_info else {}
    broker_info = broker_info or {}
    instance_type = broker_info.get("instance_type")
    if not instance_type:
        return _unpriced(tf, "missing broker_node_group_info.instance_type")
    row = price_db.get_price("AmazonMSK", region, f"msk:{instance_type}", db=db)
    if row is None:
        return _unpriced(tf, f"no MSK price data for {instance_type} in {region}")
    broker_count = int(broker_info.get("number_of_broker_nodes") or 1)
    rate = row["price_usd"]
    monthly_cost = broker_count * rate * 730
    comps = [CostComponent(
        name=f"Broker nodes ({broker_count}x {instance_type})", unit="hours",
        hourly_quantity=float(broker_count), monthly_quantity=float(broker_count) * 730,
        price=rate, hourly_cost=broker_count * rate, monthly_cost=monthly_cost, usage_based=False,
    )]

    storage_info = broker_info.get("storage_info")
    if isinstance(storage_info, list):
        storage_info = storage_info[0] if storage_info else {}
    storage_info = storage_info or {}
    ebs_info = storage_info.get("ebs_storage_info")
    if isinstance(ebs_info, list):
        ebs_info = ebs_info[0] if ebs_info else {}
    ebs_info = ebs_info or {}
    volume_size = ebs_info.get("volume_size")
    if volume_size:
        storage_row = price_db.get_price("AmazonMSK", region, "msk:storage", db=db)
        if storage_row is not None:
            storage_cost = broker_count * float(volume_size) * storage_row["price_usd"]
            comps.append(CostComponent(
                name=f"Broker storage ({broker_count}x {volume_size} GB)", unit="GB-months",
                hourly_quantity=None, monthly_quantity=float(broker_count) * float(volume_size),
                price=storage_row["price_usd"], hourly_cost=None, monthly_cost=storage_cost, usage_based=False,
            ))
            monthly_cost += storage_cost

    return Resource(
        name=tf.address, resource_type=tf.type, tags=values.get("tags") or {},
        monthly_cost=monthly_cost, hourly_cost=monthly_cost / 730,
        cost_components=comps, sub_resources=[],
    )


def _price_eip(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """
    Flat hourly rate, unconditionally — since the Feb 2024 pricing change,
    an Elastic IP costs the same whether it's attached to a running
    instance, a stopped one, or nothing at all, so there's no
    config-dependent branch here (unlike the pre-2024 model, where
    attachment state controlled the price and Terraform config alone
    couldn't tell you that anyway).
    """
    row = price_db.get_price("AmazonVPC", region, "eip:hourly", db=db)
    if row is None:
        return _unpriced(tf, f"no Elastic IP price data in {region}")
    rate = row["price_usd"]
    monthly_cost = rate * 730
    comp = CostComponent(
        name="Public IPv4 address", unit="hours",
        hourly_quantity=1.0, monthly_quantity=730.0,
        price=rate, hourly_cost=rate, monthly_cost=monthly_cost, usage_based=False,
    )
    return Resource(
        name=tf.address, resource_type=tf.type, tags=tf.values.get("tags") or {},
        monthly_cost=monthly_cost, hourly_cost=rate,
        cost_components=[comp], sub_resources=[],
    )


def _price_cloudtrail(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """
    Fully usage-based, same as Config: the volume of management, data, and
    Insights events is never in an `aws_cloudtrail` resource's own config,
    only which categories of events *can* incur charges (data/Insights
    selectors present or not). All three components are always returned
    unit-priced with no total, regardless of what's configured — a trail
    with no data-event selectors simply never accrues that component's
    usage in practice.
    """
    mgmt_row = price_db.get_price("AWSCloudTrail", region, "cloudtrail:management", db=db)
    if mgmt_row is None:
        return _unpriced(tf, f"no CloudTrail price data in {region}")
    data_row = price_db.get_price("AWSCloudTrail", region, "cloudtrail:data", db=db)
    insights_row = price_db.get_price("AWSCloudTrail", region, "cloudtrail:insights", db=db)
    comps = [
        CostComponent(
            name="Management events (beyond first free trail)", unit="100K events",
            hourly_quantity=None, monthly_quantity=None,
            price=mgmt_row["price_usd"], hourly_cost=None, monthly_cost=None, usage_based=True,
        ),
    ]
    if data_row is not None:
        comps.append(CostComponent(
            name="Data events", unit="100K events",
            hourly_quantity=None, monthly_quantity=None,
            price=data_row["price_usd"], hourly_cost=None, monthly_cost=None, usage_based=True,
        ))
    if insights_row is not None:
        comps.append(CostComponent(
            name="Insights events", unit="100K events",
            hourly_quantity=None, monthly_quantity=None,
            price=insights_row["price_usd"], hourly_cost=None, monthly_cost=None, usage_based=True,
        ))
    return Resource(
        name=tf.address, resource_type=tf.type, tags=tf.values.get("tags") or {},
        monthly_cost=None, hourly_cost=None,
        cost_components=comps, sub_resources=[],
    )


def _price_guardduty_detector(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """
    Fully usage-based: the GB of CloudTrail/DNS-log/VPC-Flow-Log/etc. data
    GuardDuty ends up analyzing isn't derivable from `aws_guardduty_detector`'s
    own config (just `enable`/`finding_publishing_frequency`). Only the base
    analysis tier is priced — see `fetch_guardduty`'s docstring for the
    unpriced additional protection plans (S3, EKS, Malware).
    """
    row = price_db.get_price("AmazonGuardDuty", region, "guardduty:analysis", db=db)
    if row is None:
        return _unpriced(tf, f"no GuardDuty price data in {region}")
    comp = CostComponent(
        name="Events analyzed", unit="GB",
        hourly_quantity=None, monthly_quantity=None,
        price=row["price_usd"], hourly_cost=None, monthly_cost=None, usage_based=True,
    )
    return Resource(
        name=tf.address, resource_type=tf.type, tags=tf.values.get("tags") or {},
        monthly_cost=None, hourly_cost=None,
        cost_components=[comp], sub_resources=[],
    )


def _price_docdb_cluster_instance(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """
    Instance-hours, config-derivable from `instance_class` — same shape as
    RDS. Cluster storage/I/O (`aws_docdb_cluster`) is usage-based and has
    no dedicated pricer; DocumentDB has only one engine, so the price key
    doesn't need an engine qualifier the way RDS's does.
    """
    instance_class = tf.values.get("instance_class")
    if not instance_class:
        return _unpriced(tf, "missing instance_class")
    row = price_db.get_price("AmazonDocDB", region, f"docdb:{instance_class}", db=db)
    if row is None:
        return _unpriced(tf, f"no DocumentDB price data for {instance_class} in {region}")
    rate = row["price_usd"]
    monthly_cost = rate * 730
    comp = CostComponent(
        name=f"Instance ({instance_class})", unit="hours",
        hourly_quantity=1.0, monthly_quantity=730.0,
        price=rate, hourly_cost=rate, monthly_cost=monthly_cost, usage_based=False,
    )
    return Resource(
        name=tf.address, resource_type=tf.type, tags=tf.values.get("tags") or {},
        monthly_cost=monthly_cost, hourly_cost=rate,
        cost_components=[comp], sub_resources=[],
    )


def _price_fsx_windows_file_system(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """
    Storage (SSD default, or HDD when `storage_type = "HDD"`) plus
    provisioned throughput capacity — both config-derivable from
    `storage_capacity`/`storage_type`/`throughput_capacity`, same shape as
    OpenSearch/MSK's instance-plus-storage pattern but with no per-node
    multiplier (FSx for Windows is single-filesystem, not a cluster of
    priced units).
    """
    values = tf.values
    storage_capacity = values.get("storage_capacity")
    throughput_capacity = values.get("throughput_capacity")
    if not storage_capacity or not throughput_capacity:
        return _unpriced(tf, "missing storage_capacity or throughput_capacity")
    storage_type = (values.get("storage_type") or "SSD").upper()
    storage_key = "fsx:windows:storage:hdd" if storage_type == "HDD" else "fsx:windows:storage:ssd"
    storage_row = price_db.get_price("AmazonFSx", region, storage_key, db=db)
    if storage_row is None:
        return _unpriced(tf, f"no FSx Windows storage price data in {region}")
    throughput_row = price_db.get_price("AmazonFSx", region, "fsx:windows:throughput", db=db)
    if throughput_row is None:
        return _unpriced(tf, f"no FSx Windows throughput price data in {region}")

    storage_cost = float(storage_capacity) * storage_row["price_usd"]
    throughput_cost = float(throughput_capacity) * throughput_row["price_usd"]
    monthly_cost = storage_cost + throughput_cost
    comps = [
        CostComponent(
            name=f"Storage ({storage_type}, {storage_capacity} GB)", unit="GB-months",
            hourly_quantity=None, monthly_quantity=float(storage_capacity),
            price=storage_row["price_usd"], hourly_cost=None, monthly_cost=storage_cost, usage_based=False,
        ),
        CostComponent(
            name=f"Throughput capacity ({throughput_capacity} MBps)", unit="MBps-months",
            hourly_quantity=None, monthly_quantity=float(throughput_capacity),
            price=throughput_row["price_usd"], hourly_cost=None, monthly_cost=throughput_cost, usage_based=False,
        ),
    ]
    return Resource(
        name=tf.address, resource_type=tf.type, tags=values.get("tags") or {},
        monthly_cost=monthly_cost, hourly_cost=monthly_cost / 730,
        cost_components=comps, sub_resources=[],
    )


def _price_acmpca_certificate_authority(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """
    The monthly per-CA fee is flat and known from config
    (`usage_mode`: "GENERAL_PURPOSE", the default, or
    "SHORT_LIVED_CERTIFICATE"), same inverse-of-S3 pattern as Secrets
    Manager/KMS. Certificate issuance count isn't in this resource's own
    config, so that component stays usage-based.
    """
    usage_mode = (tf.values.get("usage_mode") or "GENERAL_PURPOSE").upper()
    key = "acmpca:monthly:short_lived" if usage_mode == "SHORT_LIVED_CERTIFICATE" else "acmpca:monthly:general_purpose"
    row = price_db.get_price("AWSCertificateManager", region, key, db=db)
    if row is None:
        return _unpriced(tf, f"no ACM Private CA price data for {usage_mode} in {region}")
    monthly_cost = row["price_usd"]
    comps = [CostComponent(
        name=f"Private CA ({usage_mode.replace('_', ' ').title()})", unit="months",
        hourly_quantity=None, monthly_quantity=1.0,
        price=monthly_cost, hourly_cost=None, monthly_cost=monthly_cost, usage_based=False,
    )]
    cert_row = price_db.get_price("AWSCertificateManager", region, "acmpca:certificate", db=db)
    if cert_row is not None:
        comps.append(CostComponent(
            name="Certificates issued", unit="certificates",
            hourly_quantity=None, monthly_quantity=None,
            price=cert_row["price_usd"], hourly_cost=None, monthly_cost=None, usage_based=True,
        ))
    return Resource(
        name=tf.address, resource_type=tf.type, tags=tf.values.get("tags") or {},
        monthly_cost=monthly_cost, hourly_cost=monthly_cost / 730,
        cost_components=comps, sub_resources=[],
    )


def _price_athena_workgroup(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """
    Fully usage-based, same as Config/CloudTrail: bytes scanned per query
    is driven by the data and the query, not by the workgroup's own
    config. `bytes_scanned_cutoff_per_query` caps a single query, it
    doesn't set the volume.
    """
    row = price_db.get_price("AmazonAthena", region, "athena:scanned", db=db)
    if row is None:
        return _unpriced(tf, f"no Athena price data in {region}")
    comp = CostComponent(
        name="Data scanned", unit="TB",
        hourly_quantity=None, monthly_quantity=None,
        price=row["price_usd"], hourly_cost=None, monthly_cost=None, usage_based=True,
    )
    return Resource(
        name=tf.address, resource_type=tf.type, tags=tf.values.get("tags") or {},
        monthly_cost=None, hourly_cost=None,
        cost_components=[comp], sub_resources=[],
    )


def _price_vpn_connection(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """
    Site-to-Site VPN connection: flat hourly rate, the same for every
    connection regardless of the customer gateway or transit gateway it
    attaches to. Data transferred over the tunnel is standard data
    transfer pricing and isn't modeled here.
    """
    row = price_db.get_price("AmazonVPC", region, "vpn:sitetosite:hourly", db=db)
    if row is None:
        return _unpriced(tf, f"no Site-to-Site VPN price data in {region}")
    monthly_cost = row["price_usd"] * 730
    comp = CostComponent(
        name="VPN connection", unit="hours",
        hourly_quantity=1.0, monthly_quantity=730.0,
        price=row["price_usd"], hourly_cost=row["price_usd"], monthly_cost=monthly_cost, usage_based=False,
    )
    return Resource(
        name=tf.address, resource_type=tf.type, tags=tf.values.get("tags") or {},
        monthly_cost=monthly_cost, hourly_cost=row["price_usd"],
        cost_components=[comp], sub_resources=[],
    )


def _price_client_vpn_endpoint(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """
    aws_ec2_client_vpn_endpoint: fully usage-based, same as Config/Athena.
    Both components AWS actually bills — per-subnet-association hours and
    per-active-connection hours — depend on how many subnets get
    associated (a separate `aws_ec2_client_vpn_network_association`
    resource) and how many clients connect, neither of which the
    endpoint's own config reveals.
    """
    assoc_row = price_db.get_price("AmazonVPC", region, "vpn:clientvpn:association:hourly", db=db)
    conn_row = price_db.get_price("AmazonVPC", region, "vpn:clientvpn:connection:hourly", db=db)
    if assoc_row is None and conn_row is None:
        return _unpriced(tf, f"no Client VPN price data in {region}")
    comps = []
    if assoc_row is not None:
        comps.append(CostComponent(
            name="Subnet associations", unit="hours",
            hourly_quantity=None, monthly_quantity=None,
            price=assoc_row["price_usd"], hourly_cost=None, monthly_cost=None, usage_based=True,
        ))
    if conn_row is not None:
        comps.append(CostComponent(
            name="Active connections", unit="hours",
            hourly_quantity=None, monthly_quantity=None,
            price=conn_row["price_usd"], hourly_cost=None, monthly_cost=None, usage_based=True,
        ))
    return Resource(
        name=tf.address, resource_type=tf.type, tags=tf.values.get("tags") or {},
        monthly_cost=None, hourly_cost=None,
        cost_components=comps, sub_resources=[],
    )


def _price_dx_connection(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """
    aws_dx_connection / aws_dx_hosted_connection: port-hour fee is
    config-derivable from `bandwidth` (e.g. "1Gbps", "10Gbps"). Data
    transferred out over the connection is usage-based and isn't
    modeled here.
    """
    bandwidth = tf.values.get("bandwidth")
    if not bandwidth:
        return _unpriced(tf, "missing bandwidth")
    key = f"directconnect:port:{bandwidth.replace(' ', '').lower()}"
    row = price_db.get_price("AWSDirectConnect", region, key, db=db)
    if row is None:
        return _unpriced(tf, f"no Direct Connect price data for {bandwidth} in {region}")
    monthly_cost = row["price_usd"] * 730
    comp = CostComponent(
        name=f"Port ({bandwidth})", unit="hours",
        hourly_quantity=1.0, monthly_quantity=730.0,
        price=row["price_usd"], hourly_cost=row["price_usd"], monthly_cost=monthly_cost, usage_based=False,
    )
    return Resource(
        name=tf.address, resource_type=tf.type, tags=tf.values.get("tags") or {},
        monthly_cost=monthly_cost, hourly_cost=row["price_usd"],
        cost_components=[comp], sub_resources=[],
    )


def _price_appsync_graphql_api(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """
    aws_appsync_graphql_api: fully usage-based, same as Config/Athena —
    query/mutation and real-time-subscription volume depend on client
    traffic, not on the API's own config.
    """
    req_row = price_db.get_price("AWSAppSync", region, "appsync:requests", db=db)
    conn_row = price_db.get_price("AWSAppSync", region, "appsync:connectionminutes", db=db)
    if req_row is None and conn_row is None:
        return _unpriced(tf, f"no AppSync price data in {region}")
    comps = []
    if req_row is not None:
        comps.append(CostComponent(
            name="Query and data modification operations", unit="requests",
            hourly_quantity=None, monthly_quantity=None,
            price=req_row["price_usd"] * _PER_MILLION, hourly_cost=None, monthly_cost=None, usage_based=True,
        ))
    if conn_row is not None:
        comps.append(CostComponent(
            name="Real-time subscription connection-minutes", unit="minutes",
            hourly_quantity=None, monthly_quantity=None,
            price=conn_row["price_usd"], hourly_cost=None, monthly_cost=None, usage_based=True,
        ))
    return Resource(
        name=tf.address, resource_type=tf.type, tags=tf.values.get("tags") or {},
        monthly_cost=None, hourly_cost=None,
        cost_components=comps, sub_resources=[],
    )


def _price_cognito_user_pool(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """
    aws_cognito_user_pool: fully usage-based, same as Config/Athena —
    monthly active user count isn't derivable from the pool's own config.
    A single representative (first-tier) MAU rate is used; see
    `fetch_cognito`'s docstring for the tiering/advanced-security caveat.
    """
    row = price_db.get_price("AmazonCognito", region, "cognito:mau", db=db)
    if row is None:
        return _unpriced(tf, f"no Cognito price data in {region}")
    comp = CostComponent(
        name="Monthly active users", unit="users",
        hourly_quantity=None, monthly_quantity=None,
        price=row["price_usd"], hourly_cost=None, monthly_cost=None, usage_based=True,
    )
    return Resource(
        name=tf.address, resource_type=tf.type, tags=tf.values.get("tags") or {},
        monthly_cost=None, hourly_cost=None,
        cost_components=[comp], sub_resources=[],
    )


def _price_glue_job(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """
    aws_glue_job: fully usage-based, same as Config/Athena — run
    frequency and duration (and thus total DPU-hours) aren't derivable
    from the job's own config, even though `max_capacity`/`worker_type`/
    `number_of_workers` set the DPU rate *per run*.
    """
    row = price_db.get_price("AWSGlue", region, "glue:dpuhour", db=db)
    if row is None:
        return _unpriced(tf, f"no Glue price data in {region}")
    comp = CostComponent(
        name="DPU-hours", unit="DPU-hours",
        hourly_quantity=None, monthly_quantity=None,
        price=row["price_usd"], hourly_cost=None, monthly_cost=None, usage_based=True,
    )
    return Resource(
        name=tf.address, resource_type=tf.type, tags=tf.values.get("tags") or {},
        monthly_cost=None, hourly_cost=None,
        cost_components=[comp], sub_resources=[],
    )


def _price_glue_crawler(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """aws_glue_crawler: billed the same per-DPU-hour rate as Glue jobs, same usage-based shape."""
    row = price_db.get_price("AWSGlue", region, "glue:dpuhour", db=db)
    if row is None:
        return _unpriced(tf, f"no Glue price data in {region}")
    comp = CostComponent(
        name="DPU-hours", unit="DPU-hours",
        hourly_quantity=None, monthly_quantity=None,
        price=row["price_usd"], hourly_cost=None, monthly_cost=None, usage_based=True,
    )
    return Resource(
        name=tf.address, resource_type=tf.type, tags=tf.values.get("tags") or {},
        monthly_cost=None, hourly_cost=None,
        cost_components=[comp], sub_resources=[],
    )


def _price_sagemaker_notebook_instance(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """
    aws_sagemaker_notebook_instance: config-derivable, always-on
    instance-hours by `instance_type` — same shape as EC2/RDS.
    """
    instance_type = tf.values.get("instance_type")
    if not instance_type:
        return _unpriced(tf, "missing instance_type")
    row = price_db.get_price("AmazonSageMaker", region, f"sagemaker:{instance_type}", db=db)
    if row is None:
        return _unpriced(tf, f"no SageMaker price data for {instance_type} in {region}")
    monthly_cost = row["price_usd"] * 730
    comp = CostComponent(
        name=f"Notebook instance ({instance_type})", unit="hours",
        hourly_quantity=1.0, monthly_quantity=730.0,
        price=row["price_usd"], hourly_cost=row["price_usd"], monthly_cost=monthly_cost, usage_based=False,
    )
    return Resource(
        name=tf.address, resource_type=tf.type, tags=tf.values.get("tags") or {},
        monthly_cost=monthly_cost, hourly_cost=row["price_usd"],
        cost_components=[comp], sub_resources=[],
    )


def _price_sagemaker_endpoint_configuration(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """
    aws_sagemaker_endpoint_configuration: cost lives here, not on
    `aws_sagemaker_endpoint` — the endpoint resource only references a
    config by name, while `production_variants` (instance type + count
    per variant) is what actually drives instance-hours. Serverless
    variants (`serverless_config` instead of `instance_type`) have no
    dedicated instance-hour rate and are skipped with a note.
    """
    variants = tf.values.get("production_variants") or []
    if isinstance(variants, dict):
        variants = [variants]
    if not variants:
        return _unpriced(tf, "missing production_variants")
    comps = []
    monthly_cost = 0.0
    priced_any = False
    for variant in variants:
        instance_type = variant.get("instance_type")
        variant_name = variant.get("variant_name") or "default"
        if not instance_type:
            comps.append(CostComponent(
                name=f"Variant {variant_name} (serverless, not priced)", unit="hours",
                hourly_quantity=None, monthly_quantity=None,
                price=None, hourly_cost=None, monthly_cost=None, usage_based=True,
            ))
            continue
        count = int(variant.get("initial_instance_count") or 1)
        row = price_db.get_price("AmazonSageMaker", region, f"sagemaker:{instance_type}", db=db)
        if row is None:
            comps.append(CostComponent(
                name=f"Variant {variant_name} ({count}x {instance_type}, no price data)", unit="hours",
                hourly_quantity=None, monthly_quantity=None,
                price=None, hourly_cost=None, monthly_cost=None, usage_based=True,
            ))
            continue
        priced_any = True
        variant_monthly = count * row["price_usd"] * 730
        monthly_cost += variant_monthly
        comps.append(CostComponent(
            name=f"Variant {variant_name} ({count}x {instance_type})", unit="hours",
            hourly_quantity=float(count), monthly_quantity=float(count) * 730,
            price=row["price_usd"], hourly_cost=count * row["price_usd"], monthly_cost=variant_monthly, usage_based=False,
        ))
    if not priced_any:
        return _unpriced(tf, f"no SageMaker price data for any production variant in {region}")
    return Resource(
        name=tf.address, resource_type=tf.type, tags=tf.values.get("tags") or {},
        monthly_cost=monthly_cost, hourly_cost=monthly_cost / 730,
        cost_components=comps, sub_resources=[],
    )


def _price_cloudhsm_hsm(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """aws_cloudhsm_v2_hsm: flat HSM-hour rate, no instance-type variation."""
    row = price_db.get_price("CloudHSM", region, "cloudhsm:hourly", db=db)
    if row is None:
        return _unpriced(tf, f"no CloudHSM price data in {region}")
    monthly_cost = row["price_usd"] * 730
    comp = CostComponent(
        name="HSM instance", unit="hours",
        hourly_quantity=1.0, monthly_quantity=730.0,
        price=row["price_usd"], hourly_cost=row["price_usd"], monthly_cost=monthly_cost, usage_based=False,
    )
    return Resource(
        name=tf.address, resource_type=tf.type, tags=tf.values.get("tags") or {},
        monthly_cost=monthly_cost, hourly_cost=row["price_usd"],
        cost_components=[comp], sub_resources=[],
    )


def _price_cloudhsm_cluster_free(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """aws_cloudhsm_v2_cluster: carries no charge of its own — cost is on each `aws_cloudhsm_v2_hsm`."""
    comp = CostComponent(
        name="Cluster", unit="months",
        hourly_quantity=None, monthly_quantity=None,
        price=0.0, hourly_cost=0.0, monthly_cost=0.0, usage_based=False,
    )
    return Resource(
        name=tf.address, resource_type=tf.type, tags=tf.values.get("tags") or {},
        monthly_cost=0.0, hourly_cost=0.0,
        cost_components=[comp], sub_resources=[],
    )


def _price_macie_account(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """
    aws_macie2_account: fully usage-based, same as Config/Athena — how
    much S3 data gets evaluated depends on account-wide bucket contents
    and Macie's own sampling, not on the account resource's config.
    """
    row = price_db.get_price("AmazonMacie", region, "macie:gb", db=db)
    if row is None:
        return _unpriced(tf, f"no Macie price data in {region}")
    comp = CostComponent(
        name="Data evaluated (S3)", unit="GB",
        hourly_quantity=None, monthly_quantity=None,
        price=row["price_usd"], hourly_cost=None, monthly_cost=None, usage_based=True,
    )
    return Resource(
        name=tf.address, resource_type=tf.type, tags=tf.values.get("tags") or {},
        monthly_cost=None, hourly_cost=None,
        cost_components=[comp], sub_resources=[],
    )


def _price_macie_classification_job(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """aws_macie2_classification_job: same per-GB rate and usage-based shape as the account resource."""
    row = price_db.get_price("AmazonMacie", region, "macie:gb", db=db)
    if row is None:
        return _unpriced(tf, f"no Macie price data in {region}")
    comp = CostComponent(
        name="Data evaluated (S3)", unit="GB",
        hourly_quantity=None, monthly_quantity=None,
        price=row["price_usd"], hourly_cost=None, monthly_cost=None, usage_based=True,
    )
    return Resource(
        name=tf.address, resource_type=tf.type, tags=tf.values.get("tags") or {},
        monthly_cost=None, hourly_cost=None,
        cost_components=[comp], sub_resources=[],
    )


def _price_inspector_enabler(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """
    aws_inspector2_enabler: fully usage-based, same as Config/Athena —
    real cost depends on account-wide EC2 instance-months, ECR image
    scans, and Lambda function-months, none of which the enabler's own
    config reveals. One usage-based component is emitted per resource
    type in `resource_types` that has cached price data.
    """
    resource_types = tf.values.get("resource_types") or []
    if isinstance(resource_types, str):
        resource_types = [resource_types]
    key_by_type = {"EC2": "inspector:ec2", "ECR": "inspector:ecr", "LAMBDA": "inspector:lambda"}
    name_by_type = {
        "EC2": "EC2 instance scanning",
        "ECR": "ECR image scanning",
        "LAMBDA": "Lambda function scanning",
    }
    comps = []
    for rtype in resource_types:
        rtype_upper = str(rtype).upper()
        key = key_by_type.get(rtype_upper)
        if key is None:
            continue
        row = price_db.get_price("AmazonInspectorV2", region, key, db=db)
        if row is None:
            continue
        comps.append(CostComponent(
            name=name_by_type[rtype_upper], unit="months" if rtype_upper != "ECR" else "images",
            hourly_quantity=None, monthly_quantity=None,
            price=row["price_usd"], hourly_cost=None, monthly_cost=None, usage_based=True,
        ))
    if not comps:
        return _unpriced(tf, f"no Inspector price data for {resource_types} in {region}")
    return Resource(
        name=tf.address, resource_type=tf.type, tags=tf.values.get("tags") or {},
        monthly_cost=None, hourly_cost=None,
        cost_components=comps, sub_resources=[],
    )


def _price_fsx_lustre_file_system(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """
    Storage capacity is config-derivable, priced by `deployment_type` +
    `storage_type` (default SSD; PERSISTENT_1 also allows HDD). The
    within-deployment rate variation driven by `per_unit_storage_throughput`
    isn't captured — a single representative rate per deployment/storage-type
    combination is used, so a Lustre file system provisioned with unusually
    high throughput will under-report its real storage cost. Terraform's
    `deployment_type` spells deployments with an underscore (`SCRATCH_2`);
    the Pricing API's `deploymentOption` attribute doesn't (`Scratch2`) —
    stripped here so the two agree on the same price key.
    """
    values = tf.values
    storage_capacity = values.get("storage_capacity")
    deployment_type = values.get("deployment_type")
    if not storage_capacity or not deployment_type:
        return _unpriced(tf, "missing storage_capacity or deployment_type")
    storage_type = (values.get("storage_type") or "SSD").upper()
    key = f"fsx:lustre:{deployment_type.upper().replace('_', '')}:{storage_type}"
    row = price_db.get_price("AmazonFSx", region, key, db=db)
    if row is None:
        return _unpriced(tf, f"no FSx Lustre price data for {deployment_type}/{storage_type} in {region}")
    monthly_cost = float(storage_capacity) * row["price_usd"]
    comp = CostComponent(
        name=f"Storage ({deployment_type}, {storage_type}, {storage_capacity} GB)", unit="GB-months",
        hourly_quantity=None, monthly_quantity=float(storage_capacity),
        price=row["price_usd"], hourly_cost=None, monthly_cost=monthly_cost, usage_based=False,
    )
    return Resource(
        name=tf.address, resource_type=tf.type, tags=values.get("tags") or {},
        monthly_cost=monthly_cost, hourly_cost=monthly_cost / 730,
        cost_components=[comp], sub_resources=[],
    )


def _price_neptune_cluster_instance(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """Instance-hours, config-derivable from `instance_class` — same shape
    as RDS/DocumentDB. Cluster storage/I/O (`aws_neptune_cluster`) is
    usage-based and has no dedicated pricer."""
    instance_class = tf.values.get("instance_class")
    if not instance_class:
        return _unpriced(tf, "missing instance_class")
    row = price_db.get_price("AmazonNeptune", region, f"neptune:{instance_class}", db=db)
    if row is None:
        return _unpriced(tf, f"no Neptune price data for {instance_class} in {region}")
    rate = row["price_usd"]
    monthly_cost = rate * 730
    comp = CostComponent(
        name=f"Instance ({instance_class})", unit="hours",
        hourly_quantity=1.0, monthly_quantity=730.0,
        price=rate, hourly_cost=rate, monthly_cost=monthly_cost, usage_based=False,
    )
    return Resource(
        name=tf.address, resource_type=tf.type, tags=tf.values.get("tags") or {},
        monthly_cost=monthly_cost, hourly_cost=rate,
        cost_components=[comp], sub_resources=[],
    )


def _price_global_accelerator(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """
    Fixed hourly fee, flat and known from config (every standard
    accelerator costs the same regardless of listener/endpoint config) —
    plus a usage-based data-transfer-premium component, since the actual
    GB routed through the accelerator isn't in this resource's config.
    The fixed fee is global pricing, like Route 53/CloudFront, stored
    under whatever region key was requested.
    """
    row = price_db.get_price("AWSGlobalAccelerator", region, "globalaccelerator:hourly", db=db)
    if row is None:
        return _unpriced(tf, "no Global Accelerator price data")
    rate = row["price_usd"]
    monthly_cost = rate * 730
    comps = [CostComponent(
        name="Accelerator", unit="hours",
        hourly_quantity=1.0, monthly_quantity=730.0,
        price=rate, hourly_cost=rate, monthly_cost=monthly_cost, usage_based=False,
    )]
    data_row = price_db.get_price("AWSGlobalAccelerator", region, "globalaccelerator:data", db=db)
    if data_row is not None:
        comps.append(CostComponent(
            name="Data transfer premium", unit="GB",
            hourly_quantity=None, monthly_quantity=None,
            price=data_row["price_usd"], hourly_cost=None, monthly_cost=None, usage_based=True,
        ))
    return Resource(
        name=tf.address, resource_type=tf.type, tags=tf.values.get("tags") or {},
        monthly_cost=monthly_cost, hourly_cost=rate,
        cost_components=comps, sub_resources=[],
    )


def _price_mq_broker(tf: TFResource, region: str, db=None) -> Optional[Resource]:
    """
    Broker instance-hours, config-derivable from `host_instance_type` and
    a broker count inferred from `deployment_mode`
    (SINGLE_INSTANCE → 1, ACTIVE_STANDBY_MULTI_AZ/CLUSTER_MULTI_AZ → 2) —
    same shape as OpenSearch/MSK's node-count pattern. EBS storage isn't
    config-derivable (data volume, not a provisioned size, drives it for
    Amazon MQ) so it's left usage-based; the ActiveMQ-vs-RabbitMQ
    difference in whether storage is separately billed isn't modeled.
    """
    values = tf.values
    instance_type = values.get("host_instance_type")
    if not instance_type:
        return _unpriced(tf, "missing host_instance_type")
    row = price_db.get_price("AmazonMQ", region, f"mq:{instance_type}", db=db)
    if row is None:
        return _unpriced(tf, f"no Amazon MQ price data for {instance_type} in {region}")
    deployment_mode = (values.get("deployment_mode") or "SINGLE_INSTANCE").upper()
    broker_count = 1 if deployment_mode == "SINGLE_INSTANCE" else 2
    rate = row["price_usd"]
    monthly_cost = broker_count * rate * 730
    comps = [CostComponent(
        name=f"Broker instances ({broker_count}x {instance_type})", unit="hours",
        hourly_quantity=float(broker_count), monthly_quantity=float(broker_count) * 730,
        price=rate, hourly_cost=broker_count * rate, monthly_cost=monthly_cost, usage_based=False,
    )]
    storage_row = price_db.get_price("AmazonMQ", region, "mq:storage", db=db)
    if storage_row is not None:
        comps.append(CostComponent(
            name="Storage", unit="GB-months",
            hourly_quantity=None, monthly_quantity=None,
            price=storage_row["price_usd"], hourly_cost=None, monthly_cost=None, usage_based=True,
        ))
    return Resource(
        name=tf.address, resource_type=tf.type, tags=values.get("tags") or {},
        monthly_cost=monthly_cost, hourly_cost=monthly_cost / 730,
        cost_components=comps, sub_resources=[],
    )


_PRICERS = {
    "aws_instance": _price_ec2_instance,
    "aws_launch_template": _price_ec2_instance,
    "aws_ebs_volume": _price_ebs_volume,
    "aws_db_instance": _price_rds_instance,
    "aws_rds_cluster_instance": _price_rds_instance,
    "aws_lb": _price_lb,
    "aws_alb": _price_lb,
    "aws_elb": _price_lb,
    "aws_ecs_task_definition": _price_ecs_task,
    "aws_lambda_function": _price_lambda,
    "aws_elasticache_cluster": _price_elasticache,
    "aws_elasticache_replication_group": _price_elasticache,
    "aws_s3_bucket": _price_s3_bucket,
    "aws_sqs_queue": _price_sqs_queue,
    "aws_secretsmanager_secret": _price_secretsmanager_secret,
    "aws_route53_zone": _price_route53_zone,
    "aws_kms_key": _price_kms_key,
    "aws_wafv2_web_acl": _price_waf_web_acl,
    "aws_nat_gateway": _price_nat_gateway,
    "aws_config_configuration_recorder": _price_config_recorder,
    "aws_config_config_rule": _price_config_rule,
    "aws_cloudwatch_metric_alarm": _price_cloudwatch_alarm,
    "aws_cloudwatch_log_group": _price_cloudwatch_log_group,
    "aws_eks_cluster": _price_eks_cluster,
    "aws_dynamodb_table": _price_dynamodb_table,
    "aws_vpc_endpoint": _price_vpc_endpoint,
    "aws_sns_topic": _price_sns_topic,
    "aws_efs_file_system": _price_efs_file_system,
    "aws_ecr_repository": _price_ecr_repository,
    "aws_api_gateway_rest_api": _price_api_gateway_rest_api,
    "aws_apigatewayv2_api": _price_api_gateway_v2_api,
    "aws_cloudfront_distribution": _price_cloudfront_distribution,
    "aws_kinesis_stream": _price_kinesis_stream,
    "aws_sfn_state_machine": _price_sfn_state_machine,
    "aws_cloudwatch_event_bus": _price_eventbridge_event_bus,
    "aws_ec2_transit_gateway_vpc_attachment": _price_transit_gateway_attachment,
    "aws_s3files_file_system": _price_s3files_file_system,
    "aws_s3files_mount_target": _price_s3files_free_resource,
    "aws_s3files_access_point": _price_s3files_free_resource,
    "aws_opensearch_domain": _price_opensearch_domain,
    "aws_elasticsearch_domain": _price_opensearch_domain,
    "aws_redshift_cluster": _price_redshift_cluster,
    "aws_backup_vault": _price_backup_vault,
    "aws_backup_plan": _price_backup_plan_free,
    "aws_msk_cluster": _price_msk_cluster,
    "aws_eip": _price_eip,
    "aws_cloudtrail": _price_cloudtrail,
    "aws_guardduty_detector": _price_guardduty_detector,
    "aws_docdb_cluster_instance": _price_docdb_cluster_instance,
    "aws_fsx_windows_file_system": _price_fsx_windows_file_system,
    "aws_acmpca_certificate_authority": _price_acmpca_certificate_authority,
    "aws_athena_workgroup": _price_athena_workgroup,
    "aws_fsx_lustre_file_system": _price_fsx_lustre_file_system,
    "aws_neptune_cluster_instance": _price_neptune_cluster_instance,
    "aws_globalaccelerator_accelerator": _price_global_accelerator,
    "aws_mq_broker": _price_mq_broker,
    "aws_vpn_connection": _price_vpn_connection,
    "aws_ec2_client_vpn_endpoint": _price_client_vpn_endpoint,
    "aws_dx_connection": _price_dx_connection,
    "aws_dx_hosted_connection": _price_dx_connection,
    "aws_appsync_graphql_api": _price_appsync_graphql_api,
    "aws_cognito_user_pool": _price_cognito_user_pool,
    "aws_glue_job": _price_glue_job,
    "aws_glue_crawler": _price_glue_crawler,
    "aws_sagemaker_notebook_instance": _price_sagemaker_notebook_instance,
    "aws_sagemaker_endpoint_configuration": _price_sagemaker_endpoint_configuration,
    "aws_cloudhsm_v2_hsm": _price_cloudhsm_hsm,
    "aws_cloudhsm_v2_cluster": _price_cloudhsm_cluster_free,
    "aws_macie2_account": _price_macie_account,
    "aws_macie2_classification_job": _price_macie_classification_job,
    "aws_inspector2_enabler": _price_inspector_enabler,
}


def price_resources(resources: list[TFResource], region: str, db=None) -> list[Resource]:
    """Price every supported resource; unsupported types are skipped entirely.

    Each resource is priced against its own provider region when the plan's
    `configuration` resolved one (`TFResource.region`, set by tf_state.py),
    falling back to the `region` passed in (the CLI's `--region`) otherwise —
    e.g. for a plain state export with no `configuration` block, or a region
    set via a variable Terraform couldn't resolve to a literal at plan time.
    """
    priced: list[Resource] = []
    for tf in resources:
        fn = _PRICERS.get(tf.type)
        if fn is None:
            continue
        result = fn(tf, tf.region or region, db)
        if result is not None:
            priced.append(result)
    return priced


# Costs are floats derived from multiplication, so exact equality is unsafe.
_EPSILON = 1e-9


def _diff_components(
    before: Optional[Resource], after: Optional[Resource]
) -> list[CostComponent]:
    """
    Component-level deltas, matched by component name. Usage-based components
    keep a null cost (their quantity is unknown either way) but are carried
    through so an added or removed resource still shows what it consists of.
    """
    b = {c.name: c for c in (before.cost_components if before else [])}
    a = {c.name: c for c in (after.cost_components if after else [])}
    whole_resource = before is None or after is None

    out: list[CostComponent] = []
    for name in list(a) + [n for n in b if n not in a]:
        bc, ac = b.get(name), a.get(name)
        ref = ac or bc
        delta = ((ac.monthly_cost if ac else None) or 0.0) - ((bc.monthly_cost if bc else None) or 0.0)
        if not whole_resource and abs(delta) <= _EPSILON and not ref.usage_based:
            continue
        out.append(
            CostComponent(
                name=name,
                unit=ref.unit,
                hourly_quantity=None,
                monthly_quantity=ac.monthly_quantity if ac else None,
                price=ref.price,
                hourly_cost=None,
                monthly_cost=None if ref.usage_based else delta,
                usage_based=ref.usage_based,
            )
        )
    return out


def diff_resources(prior: list[Resource], planned: list[Resource]) -> list[Resource]:
    """
    Per-resource cost deltas between two priced breakdowns, matched by address.

    Each returned Resource carries the *change* in monthly cost, so a removal
    is negative. Resources whose cost is unaffected are omitted; ones that were
    added or removed are kept even at a zero delta, since a new usage-based
    resource (an S3 bucket, a Lambda) is still worth surfacing.
    """
    prior_by_name = {r.name: r for r in prior}
    planned_by_name = {r.name: r for r in planned}
    ordered = list(planned_by_name) + [n for n in prior_by_name if n not in planned_by_name]

    out: list[Resource] = []
    for name in ordered:
        before, after = prior_by_name.get(name), planned_by_name.get(name)
        delta = (
            (after.total_monthly_cost() if after else 0.0)
            - (before.total_monthly_cost() if before else 0.0)
        )
        whole_resource = before is None or after is None
        if not whole_resource and abs(delta) <= _EPSILON:
            continue
        ref = after or before
        out.append(
            Resource(
                name=name,
                resource_type=ref.resource_type,
                tags=ref.tags,
                monthly_cost=delta,
                hourly_cost=delta / 730 if delta else 0.0,
                cost_components=_diff_components(before, after),
                sub_resources=[],
                is_supported=ref.is_supported,
                no_price=ref.no_price,
                no_price_reason=ref.no_price_reason,
            )
        )
    return out


def build_output(
    resources: list[Resource],
    region: str,
    prior_resources: Optional[list[Resource]] = None,
) -> InfracostOutput:
    """
    Wrap priced resources in the same InfracostOutput schema `report` consumes.

    Passing `prior_resources` (from a plan's pre-apply state) also populates
    `past_breakdown` and `diff`, exactly as Infracost's own diff output does.
    """
    total_monthly = sum(r.total_monthly_cost() for r in resources)
    breakdown = Breakdown(
        resources=resources,
        total_hourly_cost=total_monthly / 730 if total_monthly else 0.0,
        total_monthly_cost=total_monthly,
    )

    past_breakdown = None
    diff = None
    if prior_resources is not None:
        past_total = sum(r.total_monthly_cost() for r in prior_resources)
        past_breakdown = Breakdown(
            resources=prior_resources,
            total_hourly_cost=past_total / 730 if past_total else 0.0,
            total_monthly_cost=past_total,
        )
        delta_total = total_monthly - past_total
        diff = Breakdown(
            resources=diff_resources(prior_resources, resources),
            total_hourly_cost=delta_total / 730 if delta_total else 0.0,
            total_monthly_cost=delta_total,
        )

    project = Project(
        name=f"terraform-state-{region}",
        metadata={"path": "terraform show -json", "type": "terraform_state"},
        past_breakdown=past_breakdown,
        breakdown=breakdown,
        diff=diff,
        summary={},
    )
    supported = sum(1 for r in resources if r.is_supported)
    no_price = sum(1 for r in resources if r.no_price)
    summary = {
        "totalDetectedResources": len(resources),
        "totalSupportedResources": supported,
        "totalNoPriceResources": no_price,
        "totalUnsupportedResources": len(resources) - supported - no_price,
    }
    if diff is not None:
        summary["totalChangedResources"] = len(diff.resources)
    return InfracostOutput(
        version="bucksawz-price-state-1",
        currency="USD",
        projects=[project],
        total_hourly_cost=breakdown.total_hourly_cost,
        total_monthly_cost=total_monthly,
        time_generated=datetime.now(timezone.utc).isoformat(),
        summary=summary,
    )


def build_multi_project_output(resources_by_project: dict[str, list[Resource]]) -> InfracostOutput:
    """
    Same shape as `build_output`, for a raw multi-stack export
    (`tf_state.parse_raw_multi_stack`/`parse_raw_flat`) with no plan to
    diff against — each stack becomes its own Project, and `render.py`
    already aggregates across every project into one combined report.
    """
    projects: list[Project] = []
    grand_total = 0.0
    for name, resources in resources_by_project.items():
        monthly = sum(r.total_monthly_cost() for r in resources)
        grand_total += monthly
        breakdown = Breakdown(
            resources=resources,
            total_hourly_cost=monthly / 730 if monthly else 0.0,
            total_monthly_cost=monthly,
        )
        supported = sum(1 for r in resources if r.is_supported)
        no_price = sum(1 for r in resources if r.no_price)
        projects.append(Project(
            name=name,
            metadata={"path": name, "type": "terraform_state"},
            past_breakdown=None,
            breakdown=breakdown,
            diff=None,
            summary={
                "totalDetectedResources": len(resources),
                "totalSupportedResources": supported,
                "totalNoPriceResources": no_price,
                "totalUnsupportedResources": len(resources) - supported - no_price,
            },
        ))
    return InfracostOutput(
        version="bucksawz-price-state-multi-1",
        currency="USD",
        projects=projects,
        total_hourly_cost=grand_total / 730 if grand_total else 0.0,
        total_monthly_cost=grand_total,
        time_generated=datetime.now(timezone.utc).isoformat(),
        summary={},
    )
