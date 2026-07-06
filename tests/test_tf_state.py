"""Tests for parsing `terraform show -json` output into flat resource configs."""
from pathlib import Path
from bucksawz.pricing.tf_state import parse_file, parse_json, parse_state

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
