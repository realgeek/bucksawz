"""Tests for the forecast YAML config loader/dataclass."""
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bucksawz.forecast_config import ForecastConfig, load_forecast_config


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
