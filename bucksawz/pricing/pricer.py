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
        )

    storage_row = price_db.get_price("AmazonEC2", region, f"ebs:storage:{volume_type}", db=db)
    if storage_row is None:
        return Resource(
            name=name, resource_type=resource_type, tags=tags,
            monthly_cost=None, hourly_cost=None, cost_components=[], sub_resources=[],
            is_supported=False, no_price=True,
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
