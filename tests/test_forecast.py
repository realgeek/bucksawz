"""Tests for the forecast orchestration module (bucksawz/forecast.py)."""
import json

import pytest

from bucksawz.forecast import build_forecast_payload, ensure_viewer, run_command, run_forecast
from bucksawz.forecast_config import ForecastConfig
from bucksawz.pricing import db as price_db


class _FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_run_command_success(monkeypatch):
    import bucksawz.forecast as forecast_mod

    def fake_run(command, shell, capture_output, text, cwd):
        assert shell is True
        assert cwd is None
        return _FakeResult(stdout='{"ok": true}')

    monkeypatch.setattr(forecast_mod.subprocess, "run", fake_run)
    assert run_command("some command") == {"ok": True}


def test_run_command_passes_cwd(monkeypatch):
    import bucksawz.forecast as forecast_mod

    def fake_run(command, shell, capture_output, text, cwd):
        assert cwd == "/srv/infra"
        return _FakeResult(stdout='{"ok": true}')

    monkeypatch.setattr(forecast_mod.subprocess, "run", fake_run)
    assert run_command("some command", cwd="/srv/infra") == {"ok": True}


def test_run_command_nonzero_exit(monkeypatch):
    import bucksawz.forecast as forecast_mod

    monkeypatch.setattr(
        forecast_mod.subprocess, "run",
        lambda command, shell, capture_output, text, cwd: _FakeResult(returncode=1, stderr="boom"),
    )
    with pytest.raises(RuntimeError, match="command failed"):
        run_command("some command")


def test_run_command_invalid_json(monkeypatch):
    import bucksawz.forecast as forecast_mod

    monkeypatch.setattr(
        forecast_mod.subprocess, "run",
        lambda command, shell, capture_output, text, cwd: _FakeResult(stdout="not json"),
    )
    with pytest.raises(RuntimeError, match="did not produce valid JSON"):
        run_command("some command")


def test_build_forecast_payload(tmp_path, monkeypatch):
    db_path = tmp_path / "prices.db"
    monkeypatch.setattr(price_db, "_DEFAULT_DB", db_path)

    actual_data = {"stack-a": {"resources": []}}
    proposed_data = {"stack-a": {"resources": []}, "stack-b": {"resources": []}}

    payload = build_forecast_payload(actual_data, proposed_data, "us-east-1")

    assert payload["region"] == "us-east-1"
    assert "generatedAt" in payload
    assert payload["actual"]["version"] == "bucksawz-price-state-multi-1"
    assert payload["proposed"]["projects"][1]["name"] == "stack-b"
    assert payload["newResources"] == []


def test_build_forecast_payload_applies_ce_usage_to_actual_only(tmp_path, monkeypatch):
    db_path = tmp_path / "prices.db"
    price_db.upsert("AWSDataTransfer", "us-east-1", "datatransfer:out:0", "GB", 0.09, db=db_path)
    monkeypatch.setattr(price_db, "_DEFAULT_DB", db_path)

    actual_data = {"stack-a": {"resources": []}}
    proposed_data = {"stack-a": {"resources": []}}
    ce_usage = {"data_transfer": {"internet_egress_gb_month": 1000}}

    payload = build_forecast_payload(actual_data, proposed_data, "us-east-1", ce_usage)

    actual_resources = payload["actual"]["projects"][0]["breakdown"]["resources"]
    assert any(r["resourceType"] == "aws_data_transfer" for r in actual_resources)
    proposed_resources = payload["proposed"]["projects"][0]["breakdown"]["resources"]
    assert not any(r["resourceType"] == "aws_data_transfer" for r in proposed_resources)
    assert payload["actualEstimates"]
    assert list(payload["actualEstimates"].values())[0] == pytest.approx(90.0)


def test_build_forecast_payload_no_ce_usage_leaves_estimates_empty(tmp_path, monkeypatch):
    db_path = tmp_path / "prices.db"
    monkeypatch.setattr(price_db, "_DEFAULT_DB", db_path)

    payload = build_forecast_payload({"stack-a": {"resources": []}}, {"stack-a": {"resources": []}}, "us-east-1")
    assert payload["actualEstimates"] == {}


def test_ensure_viewer_writes_bundled_template(tmp_path):
    viewer_path = ensure_viewer(tmp_path)
    assert viewer_path == tmp_path / "bucksawz_viewer.html"
    content = viewer_path.read_text()
    assert "bucksawz forecast" in content
    assert "bucksawz_manifest.json" in content


def test_run_forecast_writes_json_manifest_and_viewer(tmp_path, monkeypatch):
    import bucksawz.forecast as forecast_mod

    db_path = tmp_path / "prices.db"
    monkeypatch.setattr(price_db, "_DEFAULT_DB", db_path)

    output_dir = tmp_path / "reports"
    config = ForecastConfig(
        actual_command="echo actual",
        proposed_command="echo proposed",
        output_dir=str(output_dir),
    )

    responses = iter([
        {"stack-a": {"resources": []}},
        {"stack-a": {"resources": []}},
    ])
    monkeypatch.setattr(forecast_mod, "run_command", lambda command, cwd=None: next(responses))

    output_path = run_forecast(config)

    assert output_path.exists()
    payload = json.loads(output_path.read_text())
    assert payload["region"] == "us-east-1"

    manifest = json.loads((output_dir / "bucksawz_manifest.json").read_text())
    assert manifest["latest"] == output_path.name

    assert (output_dir / "bucksawz_viewer.html").exists()


def test_run_forecast_runs_cost_explorer_command_when_configured(tmp_path, monkeypatch):
    import bucksawz.forecast as forecast_mod

    db_path = tmp_path / "prices.db"
    monkeypatch.setattr(price_db, "_DEFAULT_DB", db_path)

    output_dir = tmp_path / "reports"
    config = ForecastConfig(
        actual_command="echo actual",
        proposed_command="echo proposed",
        cost_explorer_command="echo ce",
        output_dir=str(output_dir),
    )

    commands_run = []

    def fake_run_command(command, cwd=None):
        commands_run.append(command)
        if command == "echo ce":
            return {}
        return {"stack-a": {"resources": []}}

    monkeypatch.setattr(forecast_mod, "run_command", fake_run_command)

    run_forecast(config)

    assert commands_run == ["echo actual", "echo proposed", "echo ce"]


def test_run_forecast_passes_infra_dir_as_cwd(tmp_path, monkeypatch):
    import bucksawz.forecast as forecast_mod

    db_path = tmp_path / "prices.db"
    monkeypatch.setattr(price_db, "_DEFAULT_DB", db_path)

    output_dir = tmp_path / "reports"
    config = ForecastConfig(
        actual_command="echo actual",
        proposed_command="echo proposed",
        infra_dir="/srv/infra",
        output_dir=str(output_dir),
    )

    cwds_seen = []

    def fake_run_command(command, cwd=None):
        cwds_seen.append(cwd)
        return {"stack-a": {"resources": []}}

    monkeypatch.setattr(forecast_mod, "run_command", fake_run_command)

    run_forecast(config)

    assert cwds_seen == ["/srv/infra", "/srv/infra"]
