"""CLI-level test for the `forecast` subcommand."""
import json
import textwrap

from click.testing import CliRunner

import bucksawz.forecast as forecast_mod
from bucksawz.cli import cli
from bucksawz.pricing import db as price_db


def test_cli_forecast_writes_json_manifest_and_viewer(tmp_path, monkeypatch):
    db_path = tmp_path / "prices.db"
    monkeypatch.setattr(price_db, "_DEFAULT_DB", db_path)

    output_dir = tmp_path / "reports"
    config_path = tmp_path / "forecast.yml"
    config_path.write_text(textwrap.dedent(f"""
        actual:
          command: "echo actual"
        proposed:
          command: "echo proposed"
        output:
          dir: {output_dir}
    """))

    responses = iter([
        {"stack-a": {"resources": []}},
        {"stack-a": {"resources": []}},
    ])
    monkeypatch.setattr(forecast_mod, "run_command", lambda command: next(responses))

    result = CliRunner().invoke(cli, ["forecast", "--config", str(config_path)])

    assert result.exit_code == 0, result.output
    assert "Forecast written to" in result.output

    manifest = json.loads((output_dir / "bucksawz_manifest.json").read_text())
    latest_path = output_dir / manifest["latest"]
    assert latest_path.exists()
    assert (output_dir / "bucksawz_viewer.html").exists()


def test_cli_forecast_missing_config_command_fails_cleanly(tmp_path):
    config_path = tmp_path / "forecast.yml"
    config_path.write_text("actual:\n  command: \"echo hi\"\n")

    result = CliRunner().invoke(cli, ["forecast", "--config", str(config_path)])

    assert result.exit_code == 1
    assert "proposed.command" in result.output
