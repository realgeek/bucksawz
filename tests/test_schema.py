"""Tests for InfracostOutput schema types and JSON parsing."""
import json
import pytest
from bucksawz.schema.infracost import (
    InfracostOutput, Project, Breakdown, Resource, CostComponent,
)

MINIMAL_JSON = {
    "version": "0.2",
    "currency": "USD",
    "timeGenerated": "2026-05-13 11:07:46 EDT",
    "totalMonthlyCost": "150.00",
    "totalHourlyCost": "0.205479",
    "projects": [
        {
            "name": "my-stack",
            "metadata": {"path": "stacks/my-stack"},
            "breakdown": {
                "resources": [
                    {
                        "name": "aws_instance.web",
                        "resourceType": "aws_instance",
                        "tags": {"Env": "prod"},
                        "monthlyCost": "73.00",
                        "hourlyCost": "0.1",
                        "costComponents": [
                            {
                                "name": "Instance usage (Linux/UNIX, on-demand, t3.medium)",
                                "unit": "hours",
                                "monthlyQuantity": "730",
                                "monthlyCost": "65.70",
                                "hourlyCost": "0.09",
                                "price": "0.09",
                            },
                            {
                                "name": "CPU credits",
                                "unit": "vCPU-hours",
                                "price": "0.05",
                            },
                        ],
                        "subresources": [
                            {
                                "name": "root_block_device",
                                "resourceType": "",
                                "tags": {},
                                "costComponents": [
                                    {
                                        "name": "Storage (gp3)",
                                        "unit": "GB",
                                        "monthlyQuantity": "20",
                                        "monthlyCost": "1.60",
                                        "price": "0.08",
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "name": "aws_s3_bucket.assets",
                        "resourceType": "aws_s3_bucket",
                        "tags": {},
                        "noPrice": True,
                        "costComponents": [],
                        "subresources": [],
                    },
                ],
                "totalMonthlyCost": "150.00",
                "totalHourlyCost": "0.205479",
            },
            "summary": {},
        }
    ],
    "summary": {
        "totalDetectedResources": 2,
        "totalSupportedResources": 1,
        "totalNoPriceResources": 1,
        "totalUnsupportedResources": 0,
    },
}


def test_from_dict_roundtrip():
    output = InfracostOutput.from_dict(MINIMAL_JSON)
    assert output.version == "0.2"
    assert output.currency == "USD"
    assert output.total_monthly_cost == 150.00
    assert len(output.projects) == 1


def test_project_fields():
    output = InfracostOutput.from_dict(MINIMAL_JSON)
    p = output.projects[0]
    assert p.name == "my-stack"
    assert p.module_path() == "stacks/my-stack"
    assert p.monthly_cost() == 150.00


def test_resource_fields():
    output = InfracostOutput.from_dict(MINIMAL_JSON)
    resources = output.projects[0].breakdown.resources
    assert len(resources) == 2

    instance = resources[0]
    assert instance.name == "aws_instance.web"
    assert instance.resource_type == "aws_instance"
    assert instance.tags == {"Env": "prod"}
    assert instance.monthly_cost == 73.00
    assert len(instance.cost_components) == 2
    assert len(instance.sub_resources) == 1


def test_cost_component_usage_based():
    output = InfracostOutput.from_dict(MINIMAL_JSON)
    comps = output.projects[0].breakdown.resources[0].cost_components
    # First component has monthlyCost — not usage-based
    assert not comps[0].usage_based
    assert comps[0].monthly_cost == 65.70
    assert comps[0].monthly_quantity == 730.0
    # Second component has no monthlyCost — usage-based
    assert comps[1].usage_based
    assert comps[1].price == 0.05


def test_total_monthly_cost_rollup():
    output = InfracostOutput.from_dict(MINIMAL_JSON)
    instance = output.projects[0].breakdown.resources[0]
    # Resource has explicit monthly_cost; total_monthly_cost() should return it
    assert instance.total_monthly_cost() == 73.00


def test_no_price_resource():
    output = InfracostOutput.from_dict(MINIMAL_JSON)
    s3 = output.projects[0].breakdown.resources[1]
    assert s3.no_price is True
    assert s3.total_monthly_cost() == 0.0


def test_aws_service_from_resource_type():
    output = InfracostOutput.from_dict(MINIMAL_JSON)
    instance = output.projects[0].breakdown.resources[0]
    assert instance.aws_service() == "EC2"

    s3 = output.projects[0].breakdown.resources[1]
    assert s3.aws_service() == "S3"


def test_aws_service_fallback_to_name():
    r = Resource(
        name="aws_lb.main",
        resource_type="",  # blank — as parsed from HTML
        tags={},
        monthly_cost=18.40,
        hourly_cost=None,
        cost_components=[],
        sub_resources=[],
    )
    assert r.aws_service() == "ELB"


def test_from_json_string():
    output = InfracostOutput.from_json(json.dumps(MINIMAL_JSON))
    assert output.total_monthly_cost == 150.00
