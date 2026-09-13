"""
End-to-end tests for the settings-panel API (bucksawz/forecast_server.py)
backing `bucksawz serve`. Spins up a real ThreadingHTTPServer on an
ephemeral port so the tests exercise the actual HTTP request/response path
(headers, status codes, JSON bodies) rather than calling handler methods
directly.
"""
import http.client
import json
import textwrap
import threading

import pytest

from bucksawz.forecast_config import load_forecast_config
from bucksawz.forecast_server import build_handler_class
from bucksawz.pricing import db as price_db


@pytest.fixture
def server(tmp_path, monkeypatch):
    import http.server

    db_path = tmp_path / "prices.db"
    monkeypatch.setattr(price_db, "_DEFAULT_DB", db_path)

    config_path = tmp_path / "forecast.yml"
    config_path.write_text(textwrap.dedent("""
        actual:
          command: "echo actual"
        proposed:
          command: "echo proposed"
        output:
          dir: reports
    """))

    serve_dir = tmp_path / "reports"
    serve_dir.mkdir()

    handler = build_handler_class(str(config_path), str(serve_dir))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd, config_path, serve_dir
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def _get(httpd, path):
    conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1])
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp.status, body


def _post(httpd, path, data):
    conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1])
    body = json.dumps(data).encode()
    conn.request("POST", path, body=body, headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    resp_body = resp.read()
    conn.close()
    return resp.status, resp_body


def test_get_config_returns_current_form(server):
    httpd, config_path, serve_dir = server
    status, body = _get(httpd, "/api/config")
    assert status == 200
    form = json.loads(body)
    assert form["actualCommand"] == "echo actual"
    assert form["proposedCommand"] == "echo proposed"


def test_post_config_updates_and_preserves_output_section(server):
    httpd, config_path, serve_dir = server
    status, body = _post(httpd, "/api/config", {"region": "eu-west-1", "infraDir": "/srv/infra"})
    assert status == 200
    form = json.loads(body)
    assert form["region"] == "eu-west-1"
    assert form["infraDir"] == "/srv/infra"

    config = load_forecast_config(str(config_path))
    assert config.output_dir == "reports"
    assert config.actual_command == "echo actual"


def test_get_cache_empty_db(server):
    httpd, config_path, serve_dir = server
    status, body = _get(httpd, "/api/cache")
    assert status == 200
    cache = json.loads(body)
    assert cache["totalRows"] == 0
    assert cache["services"] == []


def test_get_cache_reports_rows(server, tmp_path):
    httpd, config_path, serve_dir = server
    price_db.upsert("AmazonEC2", "us-east-1", "ec2:t3.micro:linux:shared", "Hrs", 0.0104, db=price_db._DEFAULT_DB)
    status, body = _get(httpd, "/api/cache")
    cache = json.loads(body)
    assert cache["totalRows"] == 1
    assert cache["services"][0]["service"] == "AmazonEC2"


def test_run_endpoint_reports_error_for_bad_command(server, monkeypatch):
    import bucksawz.forecast_server as server_mod

    httpd, config_path, serve_dir = server

    def fake_run_forecast(config):
        raise RuntimeError("command failed (1): echo actual\nboom")

    monkeypatch.setattr(server_mod, "run_forecast", fake_run_forecast)
    status, body = _post(httpd, "/api/run", {})
    assert status == 400
    result = json.loads(body)
    assert result["ok"] is False
    assert "command failed" in result["error"]


def test_unknown_api_path_is_404(server):
    httpd, config_path, serve_dir = server
    status, _ = _get(httpd, "/api/nope")
    assert status == 404


def test_static_file_still_served(server):
    httpd, config_path, serve_dir = server
    (serve_dir / "hello.txt").write_text("hi")
    status, body = _get(httpd, "/hello.txt")
    assert status == 200
    assert body == b"hi"
