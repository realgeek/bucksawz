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
