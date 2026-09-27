"""Tests for `bucksawz combine-stacks` (bucksawz/combine.py) and the
combined-show-json format it emits."""
import json

import pytest

import bucksawz.combine as combine
from bucksawz.pricing.pricer import price_terraform_json
from bucksawz.pricing.tf_state import detect_format


class _Result:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


def _show(addr):
    return {"format_version": "1.0", "values": {"root_module": {"resources": [
        {"address": addr, "type": "aws_eip", "name": "x", "provider_name": "registry.opentofu.org/hashicorp/aws", "values": {}}]}}}


def _fake_run(calls, fail_on=None):
    def run(argv, capture_output, text, cwd):
        calls.append((argv, cwd))
        joined = " ".join(argv)
        if fail_on and fail_on in joined:
            return _Result(stderr="boom", returncode=1)
        if argv[0] == "terramate":
            return _Result(stdout="stacks/management\nstacks/workloads/dev/a\nstacks/workloads/dev/b\nstacks/workloads/qa\n")
        if "show" in argv:
            return _Result(stdout=json.dumps(_show("aws_eip.x")))
        return _Result()
    return run


def test_detect_format_combined_show():
    assert detect_format({"stacks/a": _show("aws_eip.x"), "stacks/b": {"format_version": "1.0"}}) == "multi_stack_show"
    assert detect_format(_show("aws_eip.x")) == "show_json"
    assert detect_format({"stacks/a": {"resources": []}}) == "raw_multi_stack"


def test_price_combined_show_makes_one_project_per_stack(tmp_path, monkeypatch):
    from bucksawz.pricing import db as price_db
    monkeypatch.setattr(price_db, "_DEFAULT_DB", tmp_path / "prices.db")
    out = price_terraform_json({"stacks/a": _show("aws_eip.x"), "stacks/b": _show("aws_eip.y")}, "us-east-1")
    assert [p.name for p in out.projects] == ["stacks/a", "stacks/b"]
    assert out.summary["totalDetectedResources"] == 2


def test_list_stacks_filters_by_dir_prefix_and_passes_tags(monkeypatch):
    calls = []
    monkeypatch.setattr(combine.subprocess, "run", _fake_run(calls))
    stacks = combine.list_stacks("/repo", dirs=["stacks/workloads/dev/", "stacks/management"], no_tags="not-ready")
    assert stacks == ["stacks/management", "stacks/workloads/dev/a", "stacks/workloads/dev/b"]
    assert calls[0] == (["terramate", "list", "--no-tags=not-ready"], "/repo")


def test_dir_filter_does_not_match_sibling_prefix(monkeypatch):
    monkeypatch.setattr(combine.subprocess, "run", _fake_run([]))
    assert combine.list_stacks("/repo", dirs=["stacks/workloads/de"]) == []


def test_state_mode_runs_only_show(monkeypatch):
    calls = []
    monkeypatch.setattr(combine.subprocess, "run", _fake_run(calls))
    combine.collect_stack("/repo", "stacks/management", "state")
    assert [c[0][2:] for c in calls] == [["show", "-json", "-no-color"]]
    assert calls[0][0][1] == "-chdir=/repo/stacks/management"


def test_plan_mode_plans_without_lock_then_shows_and_never_applies(monkeypatch):
    calls = []
    monkeypatch.setattr(combine.subprocess, "run", _fake_run(calls))
    combine.collect_stack("/repo", "stacks/management", "plan")
    verbs = [c[0][2] for c in calls]
    assert verbs == ["plan", "show"]
    assert "-lock=false" in calls[0][0]
    assert all(v not in ("apply", "destroy", "import") for v in verbs)


def test_combine_stacks_sorted_and_keyed_by_stack(monkeypatch):
    monkeypatch.setattr(combine.subprocess, "run", _fake_run([]))
    out = combine.combine_stacks("/repo", "state", dirs=["stacks/workloads/dev"])
    assert list(out) == ["stacks/workloads/dev/a", "stacks/workloads/dev/b"]


def test_failure_fails_run_unless_skip_failed(monkeypatch):
    monkeypatch.setattr(combine.subprocess, "run", _fake_run([], fail_on="stacks/workloads/dev/a"))
    with pytest.raises(combine.CombineError, match="1 of 3 stacks failed"):
        combine.combine_stacks("/repo", "state", dirs=["stacks/workloads/dev", "stacks/management"])
    out = combine.combine_stacks("/repo", "state", dirs=["stacks/workloads/dev", "stacks/management"], skip_failed=True)
    assert "stacks/workloads/dev/a" not in out and "stacks/management" in out


def test_no_matching_stacks_errors(monkeypatch):
    monkeypatch.setattr(combine.subprocess, "run", _fake_run([]))
    with pytest.raises(combine.CombineError, match="no stacks matched"):
        combine.combine_stacks("/repo", "state", dirs=["nope"])


def test_cli_combine_stacks_prints_json(monkeypatch):
    from click.testing import CliRunner
    from bucksawz.cli import cli
    monkeypatch.setattr(combine.subprocess, "run", _fake_run([]))
    result = CliRunner().invoke(cli, ["combine-stacks", "--mode", "state", "--dir", "stacks/management"])
    assert result.exit_code == 0, result.output
    assert list(json.loads(result.stdout)) == ["stacks/management"]


def test_parse_account_profiles():
    assert combine.parse_account_profiles(["stacks/workloads/dev=dev", "stacks/management=mgmt"]) == {
        "stacks/workloads/dev": "dev", "stacks/management": "mgmt",
    }
    with pytest.raises(combine.CombineError, match="PREFIX=PROFILE"):
        combine.parse_account_profiles(["no-equals-sign"])


def test_profile_for_prefers_longest_match():
    profiles = {"stacks/workloads": "generic", "stacks/workloads/dev": "dev"}
    assert combine._profile_for("stacks/workloads/dev/a", profiles) == "dev"
    assert combine._profile_for("stacks/workloads/qa", profiles) == "generic"
    assert combine._profile_for("stacks/management", profiles) is None


def test_collect_stack_wraps_with_aws_vault_when_profile_given(monkeypatch):
    calls = []
    monkeypatch.setattr(combine.subprocess, "run", _fake_run(calls))
    combine.collect_stack("/repo", "stacks/workloads/dev", "plan", profile="dev")
    for argv, _ in calls:
        assert argv[:4] == ["aws-vault", "exec", "dev", "--"]
    verbs = [c[0][6] for c in calls]
    assert verbs == ["plan", "show"]
    assert all(v not in ("apply", "destroy", "import") for v in verbs)


def test_collect_stack_no_profile_runs_unwrapped(monkeypatch):
    calls = []
    monkeypatch.setattr(combine.subprocess, "run", _fake_run(calls))
    combine.collect_stack("/repo", "stacks/management", "state")
    assert calls[0][0][0] == "tofu"


def test_combine_stacks_applies_profile_map_per_stack(monkeypatch):
    calls = []
    monkeypatch.setattr(combine.subprocess, "run", _fake_run(calls))
    combine.combine_stacks(
        "/repo", "state", dirs=["stacks/workloads/dev", "stacks/management"],
        account_profiles={"stacks/workloads/dev": "dev", "stacks/management": "mgmt"},
    )
    tofu_calls = [c for c in calls if "tofu" in c[0][0] or c[0][0] == "aws-vault"]
    wrapped_profiles = {c[0][2] for c in tofu_calls if c[0][0] == "aws-vault"}
    assert wrapped_profiles == {"dev", "mgmt"}


def test_cli_combine_stacks_account_profile_option(monkeypatch):
    from click.testing import CliRunner
    from bucksawz.cli import cli
    calls = []
    monkeypatch.setattr(combine.subprocess, "run", _fake_run(calls))
    result = CliRunner().invoke(cli, [
        "combine-stacks", "--mode", "plan", "--dir", "stacks/management",
        "--account-profile", "stacks/management=mgmt",
    ])
    assert result.exit_code == 0, result.output
    assert any(c[0][0] == "aws-vault" and c[0][2] == "mgmt" for c in calls)
