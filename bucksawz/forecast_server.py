"""
HTTP control server backing `bucksawz serve` (Phase 3). Serves the static
viewer + timestamped JSON/manifest exactly like a plain
`SimpleHTTPRequestHandler` always did, and layers a small JSON API on top
so the viewer's gear-icon settings panel can read/write the forecast
config, check the local price cache, and trigger a fresh `bucksawz
forecast` run -- all from the browser, without a second terminal.

Bound to 127.0.0.1 only (see cli.py's `serve` command). The config file
this edits already holds arbitrary shell commands trusted at
Makefile-target level (see forecast_config.py's docstring), so letting the
browser both run and edit those commands over local HTTP doesn't raise the
trust bar any further.

Every request (GET/HEAD/POST) is rejected with 403 unless its Host header
names a loopback hostname, to block DNS-rebinding attacks from web pages,
and POSTs carrying an Origin header must be same-origin (Origin == Host),
to block cross-site request forgery.

Routes:
    GET  /api/config  -> current settings-panel form fields (JSON)
    POST /api/config  -> merge posted fields into the config file, return
                         the resulting form
    GET  /api/cache   -> local AWS Pricing API cache summary
    POST /api/run     -> run `bucksawz forecast` now using the current
                         config file; any other path falls through to
                         static file serving from `serve_dir`
"""
from __future__ import annotations
import http.server
import json
from pathlib import Path
from urllib.parse import urlparse

from .forecast import run_forecast
from .forecast_config import load_forecast_config, load_forecast_form, save_forecast_form
from .pricing import db as price_db


_ALLOWED_HOSTNAMES = {"127.0.0.1", "localhost", "[::1]"}


def _host_allowed(host_header: str | None) -> bool:
    """
    DNS-rebinding defence: a page on an attacker's domain can be made to
    resolve to 127.0.0.1, but the browser still sends the attacker's
    hostname in `Host`. Only loopback hostnames are accepted (any port, so
    this works whatever `--port` is); a missing Host header is rejected.
    """
    if not host_header:
        return False
    hostname = host_header if host_header.endswith("]") else host_header.rsplit(":", 1)[0]
    return hostname.lower() in _ALLOWED_HOSTNAMES


def _cache_info() -> dict:
    p = price_db.db_path()
    if not p.exists():
        return {"path": str(p), "totalRows": 0, "services": []}
    return {"path": str(p), "totalRows": price_db.count(), "services": price_db.service_summary()}


def build_handler_class(config_path: str, serve_dir: str) -> type[http.server.SimpleHTTPRequestHandler]:
    """
    A fresh subclass per call (rather than functools.partial, as plain
    static serving uses) since `config_path`/`serve_dir` need to reach the
    request handlers as closed-over values, not constructor kwargs --
    `SimpleHTTPRequestHandler.__init__` only special-cases `directory`.
    """

    class ForecastRequestHandler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=serve_dir, **kwargs)

        def _send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _host_ok(self) -> bool:
            if _host_allowed(self.headers.get("Host")):
                return True
            self.send_error(403, "Forbidden: unexpected Host header")
            return False

        def do_HEAD(self):
            if not self._host_ok():
                return
            return super().do_HEAD()

        def do_GET(self):
            if not self._host_ok():
                return
            path = urlparse(self.path).path
            if path == "/api/config":
                return self._send_json(200, load_forecast_form(config_path))
            if path == "/api/cache":
                return self._send_json(200, _cache_info())
            return super().do_GET()

        def _origin_ok(self) -> bool:
            # Browsers always attach Origin to cross-origin POSTs; only a
            # same-origin (loopback, same host:port) page may write. No
            # Origin header means a non-browser client (curl), which isn't
            # the threat this guards against.
            origin = self.headers.get("Origin")
            if origin is None:
                return True
            if urlparse(origin).netloc == self.headers.get("Host") and _host_allowed(self.headers.get("Host")):
                return True
            self.send_error(403, "Forbidden: cross-origin request")
            return False

        def do_POST(self):
            if not self._host_ok() or not self._origin_ok():
                return
            path = urlparse(self.path).path
            if path == "/api/config":
                length = int(self.headers.get("Content-Length", 0))
                try:
                    form = json.loads(self.rfile.read(length) or b"{}")
                except json.JSONDecodeError:
                    return self._send_json(400, {"error": "request body must be JSON"})
                return self._send_json(200, save_forecast_form(config_path, form))
            if path == "/api/run":
                try:
                    config = load_forecast_config(config_path)
                    output_path = run_forecast(config)
                except (ValueError, RuntimeError, FileNotFoundError) as e:
                    return self._send_json(400, {"ok": False, "error": str(e)})
                return self._send_json(200, {"ok": True, "path": output_path.name})
            return self.send_error(404)

        def log_message(self, format, *args):  # noqa: A002 - stdlib signature
            pass  # keep serve's own click.echo status line the only server output

    return ForecastRequestHandler
