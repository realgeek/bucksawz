"""Tests for parsing `terraform show -json` output into flat resource configs."""
from pathlib import Path
from bucksawz.pricing.tf_state import parse_file, parse_json, parse_prior, parse_state

FIXTURE = Path(__file__).parent / "fixtures" / "tf_state_minimal.json"


def test_parse_file_flattens_child_modules():
    resources = parse_file(str(FIXTURE))
    addresses = {r.address for r in resources}
    assert "aws_instance.web" in addresses
    assert "module.network.aws_instance.bastion" in addresses


def test_parse_file_returns_all_top_level_types():
    resources = parse_file(str(FIXTURE))
    types = {r.type for r in resources}
    assert types == {
        "aws_instance",
        "aws_db_instance",
        "aws_lb",
        "aws_ecs_task_definition",
        "aws_lambda_function",
        "aws_s3_bucket",
    }


def test_parse_state_handles_planned_values():
    data = {
        "planned_values": {
            "root_module": {
                "resources": [
                    {
                        "address": "aws_instance.foo",
                        "type": "aws_instance",
                        "name": "foo",
                        "provider_name": "registry.terraform.io/hashicorp/aws",
                        "values": {"instance_type": "t3.micro"},
                    }
                ]
            }
        }
    }
    resources = parse_state(data)
    assert len(resources) == 1
    assert resources[0].address == "aws_instance.foo"


def test_parse_state_empty_when_no_root_module():
    assert parse_state({}) == []


def test_parse_json_matches_parse_file():
    text = FIXTURE.read_text()
    assert len(parse_json(text)) == len(parse_file(str(FIXTURE)))


def test_non_aws_provider_excluded():
    data = {
        "values": {
            "root_module": {
                "resources": [
                    {
                        "address": "random_id.suffix",
                        "type": "random_id",
                        "name": "suffix",
                        "provider_name": "registry.terraform.io/hashicorp/random",
                        "values": {},
                    }
                ]
            }
        }
    }
    assert parse_state(data) == []


# ── Multi-region: provider_config_key resolution ────────────────────────────


def _plan_with_providers(root_module_config, module_calls=None, root_resources=None):
    return {
        "planned_values": {
            "root_module": {
                "resources": root_resources or [],
                "child_modules": [
                    {
                        "address": "module.network",
                        "resources": [
                            {
                                "address": "module.network.aws_vpc.main",
                                "type": "aws_vpc",
                                "name": "main",
                                "provider_name": "registry.terraform.io/hashicorp/aws",
                                "values": {},
                            }
                        ],
                    }
                ],
            }
        },
        "configuration": {
            "provider_config": {
                "aws": {"name": "aws", "expressions": {"region": {"constant_value": "us-east-1"}}},
                "aws.west": {
                    "name": "aws",
                    "alias": "west",
                    "expressions": {"region": {"constant_value": "us-west-2"}},
                },
            },
            "root_module": root_module_config,
        },
    }


def test_root_resource_region_resolved_from_provider_config():
    root_resources = [
        {
            "address": "aws_instance.web",
            "type": "aws_instance",
            "name": "web",
            "provider_name": "registry.terraform.io/hashicorp/aws",
            "values": {},
        }
    ]
    data = _plan_with_providers(
        {"resources": [{"address": "aws_instance.web", "provider_config_key": "aws"}]},
        root_resources=root_resources,
    )
    from bucksawz.pricing.tf_state import _region_map_from_configuration

    resources = parse_state(data, _region_map_from_configuration(data))
    web = next(r for r in resources if r.address == "aws_instance.web")
    assert web.region == "us-east-1"


def test_module_resource_region_resolved_through_passed_provider_alias():
    module_config = {
        "resources": [
            {"address": "aws_vpc.main", "provider_config_key": "aws"}
        ]
    }
    data = _plan_with_providers(
        {
            "resources": [],
            "module_calls": {
                "network": {
                    "providers": {"aws": "aws.west"},
                    "module": module_config,
                }
            },
        }
    )
    from bucksawz.pricing.tf_state import _region_map_from_configuration

    region_map = _region_map_from_configuration(data)
    resources = parse_state(data, region_map)
    vpc = next(r for r in resources if r.address == "module.network.aws_vpc.main")
    assert vpc.region == "us-west-2"


def test_resource_region_none_when_not_a_literal_constant():
    from bucksawz.pricing.tf_state import _region_map_from_configuration

    data = {
        "planned_values": {
            "root_module": {
                "resources": [
                    {
                        "address": "aws_instance.web",
                        "type": "aws_instance",
                        "name": "web",
                        "provider_name": "registry.terraform.io/hashicorp/aws",
                        "values": {},
                    }
                ]
            }
        },
        "configuration": {
            "provider_config": {
                "aws": {"name": "aws", "expressions": {"region": {"references": ["var.region"]}}},
            },
            "root_module": {
                "resources": [{"address": "aws_instance.web", "provider_config_key": "aws"}]
            },
        },
    }
    region_map = _region_map_from_configuration(data)
    resources = parse_state(data, region_map)
    assert resources[0].region is None


def test_parse_prior_resolves_region_from_resource_changes_fallback():
    data = {
        "resource_changes": [
            {
                "address": "aws_instance.web",
                "type": "aws_instance",
                "name": "web",
                "provider_name": "registry.terraform.io/hashicorp/aws",
                "change": {"before": {"instance_type": "t3.micro"}},
            }
        ],
        "configuration": {
            "provider_config": {
                "aws.west": {
                    "name": "aws",
                    "alias": "west",
                    "expressions": {"region": {"constant_value": "us-west-2"}},
                }
            },
            "root_module": {
                "resources": [
                    {"address": "aws_instance.web", "provider_config_key": "aws.west"}
                ]
            },
        },
    }
    resources = parse_prior(data)
    assert resources[0].region == "us-west-2"
