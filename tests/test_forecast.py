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

    def fake_run(command, shell, capture_output, text):
        assert shell is True
        return _FakeResult(stdout='{"ok": true}')

    monkeypatch.setattr(forecast_mod.subprocess, "run", fake_run)
    assert run_command("some command") == {"ok": True}


def test_run_command_nonzero_exit(monkeypatch):
    import bucksawz.forecast as forecast_mod

    monkeypatch.setattr(
        forecast_mod.subprocess, "run",
        lambda command, shell, capture_output, text: _FakeResult(returncode=1, stderr="boom"),
    )
    with pytest.raises(RuntimeError, match="command failed"):
        run_command("some command")


def test_run_command_invalid_json(monkeypatch):
    import bucksawz.forecast as forecast_mod

    monkeypatch.setattr(
        forecast_mod.subprocess, "run",
        lambda command, shell, capture_output, text: _FakeResult(stdout="not json"),
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
    monkeypatch.setattr(forecast_mod, "run_command", lambda command: next(responses))

    output_path = run_forecast(config)

    assert output_path.exists()
    payload = json.loads(output_path.read_text())
    assert payload["region"] == "us-east-1"

    manifest = json.loads((output_dir / "bucksawz_manifest.json").read_text())
    assert manifest["latest"] == output_path.name

    assert (output_dir / "bucksawz_viewer.html").exists()
