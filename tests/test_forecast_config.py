"""Tests for the forecast YAML config loader/dataclass."""
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bucksawz.forecast_config import (
    ForecastConfig, load_forecast_config, load_forecast_form, load_output_dir, save_forecast_form,
)


def test_load_forecast_config_happy_path(tmp_path):
    config_path = tmp_path / "forecast.yml"
    config_path.write_text(textwrap.dedent("""
        actual:
          command: "tofu show -json actual.tfplan"
        proposed:
          command: "tofu show -json proposed.tfplan"
        region: us-west-2
        output:
          dir: reports
          filename: "custom_{timestamp}.json"
    """))
    config = load_forecast_config(str(config_path))
    assert config.actual_command == "tofu show -json actual.tfplan"
    assert config.proposed_command == "tofu show -json proposed.tfplan"
    assert config.region == "us-west-2"
    assert config.output_dir == "reports"
    assert config.output_filename == "custom_{timestamp}.json"


def test_load_forecast_config_with_cost_explorer_command(tmp_path):
    config_path = tmp_path / "forecast.yml"
    config_path.write_text(textwrap.dedent("""
        actual:
          command: "cmd-a"
        proposed:
          command: "cmd-b"
        cost_explorer:
          command: "aws-vault exec prod -- ./ce_usage.sh"
    """))
    config = load_forecast_config(str(config_path))
    assert config.cost_explorer_command == "aws-vault exec prod -- ./ce_usage.sh"


def test_load_forecast_config_defaults(tmp_path):
    config_path = tmp_path / "forecast.yml"
    config_path.write_text(textwrap.dedent("""
        actual:
          command: "cmd-a"
        proposed:
          command: "cmd-b"
    """))
    config = load_forecast_config(str(config_path))
    assert config.region == "us-east-1"
    assert config.output_dir == "."
    assert config.output_filename == "bucksawz_{timestamp}.json"
    assert config.cost_explorer_command is None


def test_load_forecast_config_missing_actual_command(tmp_path):
    config_path = tmp_path / "forecast.yml"
    config_path.write_text(textwrap.dedent("""
        proposed:
          command: "cmd-b"
    """))
    with pytest.raises(ValueError, match="actual.command"):
        load_forecast_config(str(config_path))


def test_load_forecast_config_missing_proposed_command(tmp_path):
    config_path = tmp_path / "forecast.yml"
    config_path.write_text(textwrap.dedent("""
        actual:
          command: "cmd-a"
    """))
    with pytest.raises(ValueError, match="proposed.command"):
        load_forecast_config(str(config_path))


def test_load_forecast_config_not_a_mapping(tmp_path):
    config_path = tmp_path / "forecast.yml"
    config_path.write_text("- just\n- a\n- list\n")
    with pytest.raises(ValueError, match="mapping"):
        load_forecast_config(str(config_path))


def test_output_path_substitutes_timestamp():
    config = ForecastConfig(actual_command="a", proposed_command="b", output_dir="out")
    now = datetime(2026, 9, 12, 13, 30, 0, tzinfo=timezone.utc)
    assert config.output_path(now) == Path("out") / "bucksawz_20260912_133000.json"


def test_manifest_and_viewer_paths():
    config = ForecastConfig(actual_command="a", proposed_command="b", output_dir="out")
    assert config.manifest_path() == Path("out") / "bucksawz_manifest.json"
    assert config.viewer_path() == Path("out") / "bucksawz_viewer.html"


def test_load_forecast_config_with_infra_dir(tmp_path):
    config_path = tmp_path / "forecast.yml"
    config_path.write_text(textwrap.dedent("""
        actual:
          command: "cmd-a"
        proposed:
          command: "cmd-b"
        infra_dir: /srv/infra
    """))
    config = load_forecast_config(str(config_path))
    assert config.infra_dir == "/srv/infra"


def test_load_forecast_form_missing_file_returns_blank_defaults(tmp_path):
    form = load_forecast_form(str(tmp_path / "does-not-exist.yml"))
    assert form == {
        "repo": "",
        "actualCommand": "",
        "proposedCommand": "",
        "costExplorerCommand": "",
        "region": "us-east-1",
        "infraDir": "",
    }


def test_load_forecast_form_reads_existing_config(tmp_path):
    config_path = tmp_path / "forecast.yml"
    config_path.write_text(textwrap.dedent("""
        actual:
          command: "cmd-a"
        proposed:
          command: "cmd-b"
        region: us-west-2
        infra_dir: /srv/infra
        cost_explorer:
          command: "ce-cmd"
    """))
    form = load_forecast_form(str(config_path))
    assert form == {
        "repo": "",
        "actualCommand": "cmd-a",
        "proposedCommand": "cmd-b",
        "costExplorerCommand": "ce-cmd",
        "region": "us-west-2",
        "infraDir": "/srv/infra",
    }


def test_save_forecast_form_creates_new_file(tmp_path):
    config_path = tmp_path / "forecast.yml"
    result = save_forecast_form(str(config_path), {
        "actualCommand": "cmd-a", "proposedCommand": "cmd-b", "region": "us-west-2",
    })
    assert result["actualCommand"] == "cmd-a"
    assert result["proposedCommand"] == "cmd-b"
    assert result["region"] == "us-west-2"
    assert load_forecast_config(str(config_path)).actual_command == "cmd-a"


def test_save_forecast_form_preserves_untouched_output_section(tmp_path):
    config_path = tmp_path / "forecast.yml"
    config_path.write_text(textwrap.dedent("""
        actual:
          command: "cmd-a"
        proposed:
          command: "cmd-b"
        output:
          dir: reports
          filename: "custom_{timestamp}.json"
    """))
    save_forecast_form(str(config_path), {"region": "eu-west-1"})
    config = load_forecast_config(str(config_path))
    assert config.output_dir == "reports"
    assert config.output_filename == "custom_{timestamp}.json"
    assert config.region == "eu-west-1"
    assert config.actual_command == "cmd-a"


def test_save_forecast_form_empty_optional_fields_remove_sections(tmp_path):
    config_path = tmp_path / "forecast.yml"
    config_path.write_text(textwrap.dedent("""
        actual:
          command: "cmd-a"
        proposed:
          command: "cmd-b"
        infra_dir: /srv/infra
        cost_explorer:
          command: "ce-cmd"
    """))
    form = save_forecast_form(str(config_path), {"infraDir": "", "costExplorerCommand": ""})
    assert form["infraDir"] == ""
    assert form["costExplorerCommand"] == ""
    config = load_forecast_config(str(config_path))
    assert config.infra_dir is None
    assert config.cost_explorer_command is None


def test_save_forecast_form_creates_missing_parent_directory(tmp_path):
    config_path = tmp_path / ".bucksawz" / "config.yml"
    assert not config_path.parent.exists()
    result = save_forecast_form(str(config_path), {"actualCommand": "cmd-a"})
    assert result["actualCommand"] == "cmd-a"
    assert config_path.exists()


def test_load_output_dir_defaults_and_missing_file(tmp_path):
    assert load_output_dir(str(tmp_path / "nope.yml")) == "."

    config_path = tmp_path / "forecast.yml"
    config_path.write_text(textwrap.dedent("""
        actual:
          command: "cmd-a"
        proposed:
          command: "cmd-b"
        output:
          dir: reports
    """))
    assert load_output_dir(str(config_path)) == "reports"


_MULTI = """
    region: us-east-1
    output:
      dir: reports
      filename: "f_{timestamp}.json"
    cost_explorer:
      command: "ce-common"
    repos:
      network:
        infra_dir: /n
        actual: {command: "a-net"}
        proposed: {command: "p-net"}
      apps:
        region: eu-west-1
        actual: {command: "a-apps"}
        proposed: {command: "p-apps"}
        cost_explorer: {command: "ce-apps"}
        output: {dir: custom}
"""


def test_repos_merge_over_common_settings(tmp_path):
    path = tmp_path / "c.yml"
    path.write_text(textwrap.dedent(_MULTI))

    net = load_forecast_config(str(path), repo="network")
    assert (net.actual_command, net.region, net.infra_dir) == ("a-net", "us-east-1", "/n")
    assert net.cost_explorer_command == "ce-common"
    assert net.output_dir == str(Path("reports") / "network")
    assert net.output_filename == "f_{timestamp}.json"

    apps = load_forecast_config(str(path), repo="apps")
    assert (apps.region, apps.cost_explorer_command, apps.output_dir) == ("eu-west-1", "ce-apps", "custom")
    assert apps.output_filename == "f_{timestamp}.json"  # merged, not replaced


def test_repos_selection_errors(tmp_path):
    path = tmp_path / "c.yml"
    path.write_text(textwrap.dedent(_MULTI))
    with pytest.raises(ValueError, match="choose one with --repo"):
        load_forecast_config(str(path))
    with pytest.raises(ValueError, match="unknown repo"):
        load_forecast_config(str(path), repo="nope")

    flat = tmp_path / "flat.yml"
    flat.write_text("actual: {command: a}\nproposed: {command: b}\n")
    with pytest.raises(ValueError, match="no 'repos' section"):
        load_forecast_config(str(flat), repo="x")


def test_repo_required_even_when_only_one_defined(tmp_path):
    path = tmp_path / "c.yml"
    path.write_text("repos:\n  workiac:\n    actual: {command: a}\n    proposed: {command: b}\n")
    with pytest.raises(ValueError, match="choose one with --repo"):
        load_forecast_config(str(path))
    assert load_forecast_config(str(path), repo="workiac").actual_command == "a"


def test_load_output_dir_per_repo(tmp_path):
    path = tmp_path / "c.yml"
    path.write_text(textwrap.dedent(_MULTI))
    assert load_output_dir(str(path), repo="network") == str(Path("reports") / "network")
    assert load_output_dir(str(path), repo="apps") == "custom"
    with pytest.raises(ValueError, match="choose one with --repo"):
        load_output_dir(str(path))


def test_form_load_shows_repo_effective_values(tmp_path):
    path = tmp_path / "c.yml"
    path.write_text(textwrap.dedent(_MULTI))
    form = load_forecast_form(str(path), repo="apps")
    assert (form["repo"], form["region"], form["actualCommand"], form["costExplorerCommand"]) == (
        "apps", "eu-west-1", "a-apps", "ce-apps")
    net = load_forecast_form(str(path), repo="network")
    assert (net["costExplorerCommand"], net["region"]) == ("ce-common", "us-east-1")  # inherited
    assert load_forecast_form(str(path), repo="new")["region"] == "us-east-1"  # unsaved repo


def test_form_save_writes_into_repo_section_only(tmp_path):
    import yaml
    path = tmp_path / "c.yml"
    path.write_text(textwrap.dedent(_MULTI))
    result = save_forecast_form(str(path), {"region": "ap-south-1", "actualCommand": "new-a"}, repo="network")
    assert (result["region"], result["actualCommand"]) == ("ap-south-1", "new-a")
    data = yaml.safe_load(path.read_text())
    assert data["region"] == "us-east-1"                        # common untouched
    assert data["repos"]["network"]["region"] == "ap-south-1"
    assert data["repos"]["apps"]["actual"]["command"] == "a-apps"  # other repo untouched
    assert load_forecast_config(str(path), repo="network").region == "ap-south-1"


def test_form_save_creates_new_repo_entry(tmp_path):
    path = tmp_path / "c.yml"
    save_forecast_form(str(path), {"actualCommand": "a", "proposedCommand": "b"}, repo="workiac")
    assert load_forecast_config(str(path), repo="workiac").proposed_command == "b"


def test_load_output_dir_tolerates_unsaved_repo(tmp_path):
    flat = tmp_path / "flat.yml"
    flat.write_text("output: {dir: reports}\nactual: {command: a}\nproposed: {command: b}\n")
    assert load_output_dir(str(flat), repo="workiac") == str(Path("reports") / "workiac")
    assert load_output_dir(str(tmp_path / "nope.yml"), repo="workiac") == str(Path(".") / "workiac")
    assert load_output_dir(str(flat)) == "reports"


def test_infra_dir_tilde_is_expanded(tmp_path):
    path = tmp_path / "c.yml"
    path.write_text("infra_dir: ~/infra\nactual: {command: a}\nproposed: {command: b}\n")
    assert load_forecast_config(str(path)).infra_dir == str(Path.home() / "infra")


def test_resolve_config_path(tmp_path, monkeypatch):
    from bucksawz.forecast_config import resolve_config_path
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert resolve_config_path("/x/c.yml") == "/x/c.yml"
    assert resolve_config_path() == str(tmp_path / ".bucksawz" / "config.yml")  # create target
    (tmp_path / ".bucksawz").mkdir()
    (tmp_path / ".bucksawz" / "config.yaml").write_text("")
    assert resolve_config_path() == str(tmp_path / ".bucksawz" / "config.yaml")
    (tmp_path / ".bucksawz" / "config.yml").write_text("")
    assert resolve_config_path() == str(tmp_path / ".bucksawz" / "config.yml")  # .yml preferred
