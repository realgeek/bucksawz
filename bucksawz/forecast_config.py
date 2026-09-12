"""
Config file for `bucksawz forecast`: shell commands that produce the
"actual" (currently deployed) and "proposed" (fully deployed, e.g. with
`not-ready`-tagged resources included) terraform state/plan JSON, plus
where to write the priced comparison output. YAML:

    actual:
      command: "tofu show -json actual.tfplan"
    proposed:
      command: "tofu show -json proposed.tfplan"
    region: us-east-1
    output:
      dir: reports
      filename: "bucksawz_{timestamp}.json"   # default shown

Commands run through the user's own shell (not split into argv), since
they're commonly wrapped in aws-vault/direnv/pipes -- the config file is
trusted, user-authored input, the same trust level as a Makefile target.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import yaml

DEFAULT_OUTPUT_FILENAME = "bucksawz_{timestamp}.json"
DEFAULT_TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"
MANIFEST_FILENAME = "bucksawz_manifest.json"
VIEWER_FILENAME = "bucksawz_viewer.html"


@dataclass
class ForecastConfig:
    actual_command: str
    proposed_command: str
    region: str = "us-east-1"
    output_dir: str = "."
    output_filename: str = DEFAULT_OUTPUT_FILENAME
    timestamp_format: str = DEFAULT_TIMESTAMP_FORMAT

    def output_path(self, now: Optional[datetime] = None) -> Path:
        ts = (now or datetime.now(timezone.utc)).strftime(self.timestamp_format)
        return Path(self.output_dir) / self.output_filename.format(timestamp=ts)

    def manifest_path(self) -> Path:
        return Path(self.output_dir) / MANIFEST_FILENAME

    def viewer_path(self) -> Path:
        return Path(self.output_dir) / VIEWER_FILENAME


def load_forecast_config(path: str) -> ForecastConfig:
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"forecast config must be a YAML mapping, got {type(data).__name__}")

    def _command(key: str) -> str:
        section = data.get(key)
        if not isinstance(section, dict) or not section.get("command"):
            raise ValueError(f"forecast config missing required '{key}.command'")
        return section["command"]

    output = data.get("output") or {}
    return ForecastConfig(
        actual_command=_command("actual"),
        proposed_command=_command("proposed"),
        region=data.get("region", "us-east-1"),
        output_dir=output.get("dir", "."),
        output_filename=output.get("filename", DEFAULT_OUTPUT_FILENAME),
    )
