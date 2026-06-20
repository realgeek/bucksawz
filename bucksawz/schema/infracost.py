"""
Infracost JSON output schema types.
Schema reference: https://github.com/infracost/infracost/blob/master/schema/
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import json


@dataclass
class CostComponent:
    name: str
    unit: str
    hourly_quantity: Optional[float]
    monthly_quantity: Optional[float]
    price: Optional[float]
    hourly_cost: Optional[float]
    monthly_cost: Optional[float]
    usage_based: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> "CostComponent":
        mq = d.get("monthlyQuantity")
        hq = d.get("hourlyQuantity")
        price = d.get("price")
        mc = d.get("monthlyCost")
        hc = d.get("hourlyCost")
        usage_based = (mc is None and price is not None) or d.get("usageBased", False)
        return cls(
            name=d.get("name", ""),
            unit=d.get("unit", ""),
            hourly_quantity=float(hq) if hq is not None else None,
            monthly_quantity=float(mq) if mq is not None else None,
            price=float(price) if price is not None else None,
            hourly_cost=float(hc) if hc is not None else None,
            monthly_cost=float(mc) if mc is not None else None,
            usage_based=usage_based,
        )


@dataclass
class Resource:
    name: str
    resource_type: str
    tags: dict[str, str]
    monthly_cost: Optional[float]
    hourly_cost: Optional[float]
    cost_components: list[CostComponent]
    sub_resources: list["Resource"]
    is_supported: bool = True
    no_price: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> "Resource":
        mc = d.get("monthlyCost")
        hc = d.get("hourlyCost")
        return cls(
            name=d.get("name", ""),
            resource_type=d.get("resourceType", ""),
            tags=d.get("tags") or {},
            monthly_cost=float(mc) if mc is not None else None,
            hourly_cost=float(hc) if hc is not None else None,
            cost_components=[CostComponent.from_dict(c) for c in d.get("costComponents") or []],
            sub_resources=[Resource.from_dict(r) for r in d.get("subresources") or []],
            is_supported=d.get("isSupported", True),
            no_price=d.get("noPrice", False),
        )

    def total_monthly_cost(self) -> float:
        if self.monthly_cost is not None:
            return self.monthly_cost
        total = sum(
            (c.monthly_cost or 0.0) for c in self.cost_components if c.monthly_cost is not None
        )
        total += sum(r.total_monthly_cost() for r in self.sub_resources)
        return total

    def aws_service(self) -> str:
        """Best-effort service category from resource_type or name prefix."""
        rt = (self.resource_type or self.name.split(".")[0]).lower()
        if rt.startswith("aws_instance") or rt.startswith("aws_launch"):
            return "EC2"
        if "lb" in rt or "alb" in rt or "elb" in rt or "nlb" in rt:
            return "ELB"
        if rt.startswith("aws_rds") or rt.startswith("aws_db_"):
            return "RDS"
        if rt.startswith("aws_s3"):
            return "S3"
        if rt.startswith("aws_lambda"):
            return "Lambda"
        if rt.startswith("aws_cloudfront"):
            return "CloudFront"
        if rt.startswith("aws_route53"):
            return "Route 53"
        if rt.startswith("aws_sqs"):
            return "SQS"
        if rt.startswith("aws_sns"):
            return "SNS"
        if rt.startswith("aws_ecs") or rt.startswith("aws_ecr"):
            return "ECS/ECR"
        if rt.startswith("aws_elasticache"):
            return "ElastiCache"
        if rt.startswith("aws_cloudwatch"):
            return "CloudWatch"
        if rt.startswith("aws_nat"):
            return "NAT Gateway"
        if rt.startswith("aws_codebuild") or rt.startswith("aws_codecommit"):
            return "CodeBuild"
        if rt.startswith("aws_autoscaling"):
            return "EC2 Auto Scaling"
        if rt.startswith("aws_ebs") or "volume" in rt or "snapshot" in rt:
            return "EBS"
        if rt.startswith("aws_secretsmanager") or rt.startswith("aws_ssm"):
            return "Secrets/SSM"
        if rt.startswith("aws_waf"):
            return "WAF"
        if rt.startswith("aws_apigateway") or rt.startswith("aws_api_gateway"):
            return "API Gateway"
        return "Other"


@dataclass
class Project:
    name: str
    metadata: dict
    past_breakdown: Optional["Breakdown"]
    breakdown: Optional["Breakdown"]
    diff: Optional["Breakdown"]
    summary: dict

    @classmethod
    def from_dict(cls, d: dict) -> "Project":
        def maybe_breakdown(key):
            v = d.get(key)
            return Breakdown.from_dict(v) if v else None

        return cls(
            name=d.get("name", ""),
            metadata=d.get("metadata") or {},
            past_breakdown=maybe_breakdown("pastBreakdown"),
            breakdown=maybe_breakdown("breakdown"),
            diff=maybe_breakdown("diff"),
            summary=d.get("summary") or {},
        )

    def monthly_cost(self) -> float:
        if self.breakdown:
            return self.breakdown.total_monthly_cost or 0.0
        return 0.0

    def module_path(self) -> str:
        return self.metadata.get("path", self.name)


@dataclass
class Breakdown:
    resources: list[Resource]
    total_hourly_cost: Optional[float]
    total_monthly_cost: Optional[float]

    @classmethod
    def from_dict(cls, d: dict) -> "Breakdown":
        tmc = d.get("totalMonthlyCost")
        thc = d.get("totalHourlyCost")
        return cls(
            resources=[Resource.from_dict(r) for r in d.get("resources") or []],
            total_hourly_cost=float(thc) if thc is not None else None,
            total_monthly_cost=float(tmc) if tmc is not None else None,
        )


@dataclass
class InfracostOutput:
    version: str
    currency: str
    projects: list[Project]
    total_hourly_cost: Optional[float]
    total_monthly_cost: Optional[float]
    time_generated: str
    summary: dict

    @classmethod
    def from_dict(cls, d: dict) -> "InfracostOutput":
        tmc = d.get("totalMonthlyCost")
        thc = d.get("totalHourlyCost")
        return cls(
            version=d.get("version", ""),
            currency=d.get("currency", "USD"),
            projects=[Project.from_dict(p) for p in d.get("projects") or []],
            total_hourly_cost=float(thc) if thc is not None else None,
            total_monthly_cost=float(tmc) if tmc is not None else None,
            time_generated=d.get("timeGenerated", ""),
            summary=d.get("summary") or {},
        )

    @classmethod
    def from_json(cls, text: str) -> "InfracostOutput":
        return cls.from_dict(json.loads(text))

    @classmethod
    def from_file(cls, path: str) -> "InfracostOutput":
        with open(path) as f:
            return cls.from_dict(json.load(f))
