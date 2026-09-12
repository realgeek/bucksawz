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
    cost_explorer:                             # optional (Phase 2)
      command: "aws-vault exec prod -- ./scripts/ce_usage.sh"

Commands run through the user's own shell (not split into argv), since
they're commonly wrapped in aws-vault/direnv/pipes -- the config file is
trusted, user-authored input, the same trust level as a Makefile target.

`cost_explorer.command`'s stdout must be JSON matching (every key
optional):

    {
      "data_transfer": {"internet_egress_gb_month": 5000, "inter_az_gb_month": 100},
      "s3_storage": {"storage_gb": 500},
      "elb_usage": {"lcu_hours_month": 200},
      "rds_storage": {"storage_gb": 300},
      "ec2_runtime": {"instance_hours_month": 400},
      "elasticache_runtime": {"node_hours_month": 700}
    }

bucksawz never calls AWS directly for this -- the command is whatever
aws-vault/SSO-wrapped Cost Explorer query the user already runs, shaped
into this schema by their own script. See
`pricing.pricer.apply_cost_explorer_actuals` for how each key is used.
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
    cost_explorer_command: Optional[str] = None

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
    cost_explorer = data.get("cost_explorer") or {}
    return ForecastConfig(
        actual_command=_command("actual"),
        proposed_command=_command("proposed"),
        region=data.get("region", "us-east-1"),
        output_dir=output.get("dir", "."),
        output_filename=output.get("filename", DEFAULT_OUTPUT_FILENAME),
        cost_explorer_command=cost_explorer.get("command") if isinstance(cost_explorer, dict) else None,
    )
