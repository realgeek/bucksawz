"""Tests for plan diffing: prior-state parsing, cost deltas, and diff rendering."""
import pytest
from pathlib import Path
from bucksawz.pricing import db as price_db
from bucksawz.pricing.pricer import build_output, diff_resources, price_resources
from bucksawz.pricing.tf_state import is_plan, parse_prior, parse_state
from bucksawz.report.render import _changes, _diff_total, _fmt_delta
from bucksawz.schema.infracost import CostComponent, Resource


@pytest.fixture
def tmp_db(tmp_path) -> Path:
    db = tmp_path / "test_prices.db"
    price_db.upsert("AmazonEC2", "us-east-1", "ec2:t3.micro:linux:shared", "Hrs", 0.0104, db=db)
    price_db.upsert("AmazonEC2", "us-east-1", "ec2:m5.large:linux:shared", "Hrs", 0.096, db=db)
    price_db.upsert("AmazonS3", "us-east-1", "s3:storage:standard", "GB-Mo", 0.023, db=db)
    return db


def _resource(name, monthly_cost, components=None, resource_type="aws_instance"):
    return Resource(
        name=name,
        resource_type=resource_type,
        tags={},
        monthly_cost=monthly_cost,
        hourly_cost=None,
        cost_components=components or [],
        sub_resources=[],
    )


def _comp(name, monthly_cost, unit="hours", price=0.01, usage_based=False):
    return CostComponent(
        name=name, unit=unit,
        hourly_quantity=None, monthly_quantity=None,
        price=price, hourly_cost=None, monthly_cost=monthly_cost,
        usage_based=usage_based,
    )


def _plan(before_resources, after_resources):
    """Minimal `terraform show -json tfplan` shape: prior_state + planned_values."""
    def _mod(resources):
        return {"root_module": {"resources": resources}}

    return {
        "format_version": "1.2",
        "prior_state": {"format_version": "1.0", "values": _mod(before_resources)},
        "planned_values": _mod(after_resources),
        "resource_changes": [],
    }


def _tf(address, type_, values):
    return {
        "address": address, "type": type_, "name": address.split(".")[-1],
        "provider_name": "registry.terraform.io/hashicorp/aws", "values": values,
    }


# ── Plan detection and prior-state parsing ───────────────────────────────────


def test_is_plan_true_for_plan_export():
    assert is_plan(_plan([], []))


def test_is_plan_false_for_state_export():
    state = {"format_version": "1.0", "values": {"root_module": {"resources": []}}}
    assert not is_plan(state)


def test_parse_prior_reads_prior_state():
    plan = _plan(
        [_tf("aws_instance.web", "aws_instance", {"instance_type": "t3.micro"})],
        [_tf("aws_instance.web", "aws_instance", {"instance_type": "m5.large"})],
    )
    [prior] = parse_prior(plan)
    assert prior.values["instance_type"] == "t3.micro"
    [planned] = parse_state(plan)
    assert planned.values["instance_type"] == "m5.large"


def test_parse_prior_falls_back_to_resource_changes():
    """Some exports carry resource_changes without a prior_state."""
    plan = {
        "format_version": "1.2",
        "planned_values": {"root_module": {"resources": []}},
        "resource_changes": [
            {
                "address": "aws_instance.web", "type": "aws_instance", "name": "web",
                "provider_name": "registry.terraform.io/hashicorp/aws",
                "change": {"actions": ["delete"],
                           "before": {"instance_type": "t3.micro"}, "after": None},
            },
            {
                "address": "aws_instance.new", "type": "aws_instance", "name": "new",
                "provider_name": "registry.terraform.io/hashicorp/aws",
                "change": {"actions": ["create"],
                           "before": None, "after": {"instance_type": "m5.large"}},
            },
        ],
    }
    prior = parse_prior(plan)
    assert [r.address for r in prior] == ["aws_instance.web"]


def test_parse_prior_empty_on_first_apply():
    """No prior_state and every change is a create."""
    plan = {"format_version": "1.2", "planned_values": {"root_module": {"resources": []}},
            "resource_changes": [{"address": "aws_instance.web", "type": "aws_instance",
                                  "name": "web", "provider_name": "aws",
                                  "change": {"actions": ["create"], "before": None,
                                             "after": {"instance_type": "t3.micro"}}}]}
    assert parse_prior(plan) == []


def test_parse_prior_ignores_non_aws_providers():
    plan = {
        "format_version": "1.2",
        "resource_changes": [{
            "address": "random_id.x", "type": "random_id", "name": "x",
            "provider_name": "registry.terraform.io/hashicorp/random",
            "change": {"actions": ["update"], "before": {"byte_length": 4},
                       "after": {"byte_length": 8}},
        }],
    }
    assert parse_prior(plan) == []


# ── diff_resources ───────────────────────────────────────────────────────────


def test_diff_detects_added_resource():
    [d] = diff_resources([], [_resource("aws_instance.web", 7.59)])
    assert d.name == "aws_instance.web"
    assert d.monthly_cost == pytest.approx(7.59)


def test_diff_detects_removed_resource_as_negative():
    [d] = diff_resources([_resource("aws_instance.web", 7.59)], [])
    assert d.monthly_cost == pytest.approx(-7.59)


def test_diff_detects_resized_resource():
    prior = [_resource("aws_instance.web", 7.59)]
    planned = [_resource("aws_instance.web", 70.08)]
    [d] = diff_resources(prior, planned)
    assert d.monthly_cost == pytest.approx(70.08 - 7.59)


def test_diff_omits_unchanged_resources():
    unchanged = [_resource("aws_instance.web", 7.59)]
    assert diff_resources(unchanged, [_resource("aws_instance.web", 7.59)]) == []


def test_diff_keeps_added_resource_with_no_fixed_cost():
    """A new S3 bucket costs real money — a zero delta shouldn't hide it."""
    bucket = _resource(
        "aws_s3_bucket.assets", None,
        [_comp("Standard storage", None, unit="GB-months", price=0.023, usage_based=True)],
        resource_type="aws_s3_bucket",
    )
    [d] = diff_resources([], [bucket])
    assert d.monthly_cost == pytest.approx(0.0)
    assert d.cost_components[0].usage_based


def test_diff_reports_component_level_deltas():
    prior = [_resource("aws_lb.main", 16.42, [_comp("Application load balancer", 16.42)])]
    planned = [_resource("aws_lb.main", 17.74, [_comp("Application load balancer", 17.74)])]
    [d] = diff_resources(prior, planned)
    [comp] = d.cost_components
    assert comp.monthly_cost == pytest.approx(17.74 - 16.42)


def test_diff_omits_unchanged_components_of_changed_resources():
    prior = [_resource("aws_instance.web", 17.59,
                       [_comp("Instance usage", 7.59), _comp("EBS", 10.0)])]
    planned = [_resource("aws_instance.web", 80.08,
                         [_comp("Instance usage", 70.08), _comp("EBS", 10.0)])]
    [d] = diff_resources(prior, planned)
    assert [c.name for c in d.cost_components] == ["Instance usage"]


def test_diff_ordering_puts_removals_last():
    prior = [_resource("aws_instance.old", 5.0)]
    planned = [_resource("aws_instance.new", 9.0)]
    assert [d.name for d in diff_resources(prior, planned)] == [
        "aws_instance.new", "aws_instance.old",
    ]


# ── build_output with a prior state ──────────────────────────────────────────


def test_build_output_without_prior_has_no_diff():
    output = build_output([_resource("aws_instance.web", 7.59)], "us-east-1")
    assert output.projects[0].diff is None
    assert output.projects[0].past_breakdown is None


def test_build_output_populates_past_breakdown_and_diff():
    prior = [_resource("aws_instance.web", 7.59)]
    planned = [_resource("aws_instance.web", 70.08)]
    output = build_output(planned, "us-east-1", prior_resources=prior)
    project = output.projects[0]
    assert project.past_breakdown.total_monthly_cost == pytest.approx(7.59)
    assert project.breakdown.total_monthly_cost == pytest.approx(70.08)
    assert project.diff.total_monthly_cost == pytest.approx(70.08 - 7.59)
    assert output.summary["totalChangedResources"] == 1


def test_build_output_diff_total_is_negative_when_tearing_down():
    output = build_output([], "us-east-1", prior_resources=[_resource("aws_instance.web", 7.59)])
    assert output.projects[0].diff.total_monthly_cost == pytest.approx(-7.59)


def test_build_output_empty_prior_means_everything_is_new():
    output = build_output([_resource("aws_instance.web", 7.59)], "us-east-1", prior_resources=[])
    assert output.projects[0].diff.total_monthly_cost == pytest.approx(7.59)
    assert output.summary["totalChangedResources"] == 1


# ── End to end: plan JSON → priced diff ──────────────────────────────────────


def test_priced_plan_diff_end_to_end(tmp_db):
    plan = _plan(
        [_tf("aws_instance.web", "aws_instance", {"instance_type": "t3.micro"})],
        [
            _tf("aws_instance.web", "aws_instance", {"instance_type": "m5.large"}),
            _tf("aws_s3_bucket.assets", "aws_s3_bucket", {}),
        ],
    )
    planned = price_resources(parse_state(plan), "us-east-1", db=tmp_db)
    prior = price_resources(parse_prior(plan), "us-east-1", db=tmp_db)
    output = build_output(planned, "us-east-1", prior_resources=prior)

    expected = (0.096 * 730) - (0.0104 * 730)
    assert output.projects[0].diff.total_monthly_cost == pytest.approx(expected)

    rows = _changes(output.projects)
    by_name = {r["name"]: r for r in rows}
    assert by_name["aws_instance.web"]["change"] == "changed"
    assert by_name["aws_instance.web"]["before"] == pytest.approx(0.0104 * 730)
    assert by_name["aws_instance.web"]["after"] == pytest.approx(0.096 * 730)
    assert by_name["aws_s3_bucket.assets"]["change"] == "added"
    assert by_name["aws_s3_bucket.assets"]["usage_based_only"]


# ── Report helpers ───────────────────────────────────────────────────────────


def test_changes_empty_without_diff():
    output = build_output([_resource("aws_instance.web", 7.59)], "us-east-1")
    assert _changes(output.projects) == []
    assert _diff_total(output.projects) is None


def test_changes_classifies_removed():
    output = build_output([], "us-east-1", prior_resources=[_resource("aws_instance.web", 7.59)])
    [row] = _changes(output.projects)
    assert row["change"] == "removed"
    assert row["after"] is None
    assert row["delta"] == pytest.approx(-7.59)


def test_changes_sorted_by_magnitude():
    prior = [_resource("aws_instance.big", 500.0)]
    planned = [_resource("aws_instance.small", 5.0), _resource("aws_instance.mid", 50.0)]
    output = build_output(planned, "us-east-1", prior_resources=prior)
    assert [r["name"] for r in _changes(output.projects)] == [
        "aws_instance.big", "aws_instance.mid", "aws_instance.small",
    ]


def test_diff_total_sums_across_projects():
    a = build_output([_resource("a", 10.0)], "us-east-1", prior_resources=[]).projects[0]
    b = build_output([], "us-east-1", prior_resources=[_resource("b", 4.0)]).projects[0]
    assert _diff_total([a, b]) == pytest.approx(6.0)


@pytest.mark.parametrize("value,expected", [
    (12.5, "+$12.50"),
    (-12.5, "−$12.50"),
    (0.0, "$0.00"),
    (0.001, "$0.00"),
    (None, ""),
])
def test_fmt_delta(value, expected):
    assert _fmt_delta(value) == expected
