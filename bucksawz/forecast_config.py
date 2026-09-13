"""
Config file for `bucksawz forecast`: shell commands that produce the
"actual" (currently deployed) and "proposed" (fully deployed, e.g. with
`not-ready`-tagged resources included) terraform state/plan JSON, plus
where to write the priced comparison output. Both `bucksawz forecast` and
`bucksawz serve` default `--config` to `.bucksawz/config.yml` (relative to
cwd, typically the infra repo itself) so each infra repo's settings live
in their own hidden directory rather than sharing one path across
projects; `save_forecast_form` creates that directory if it's missing.
Pass `--config` explicitly to use a different path. YAML:

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
    infra_dir: /path/to/terraform/project       # optional (Phase 3); cwd for all three commands

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
    infra_dir: Optional[str] = None

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
        infra_dir=data.get("infra_dir"),
    )


def _read_yaml_dict(path: str) -> dict:
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        data = {}
    return data if isinstance(data, dict) else {}


def load_output_dir(path: str) -> str:
    """The configured output.dir (default '.'), tolerating a missing file --
    used by `bucksawz serve --dir`'s default so it matches wherever
    `bucksawz forecast` actually writes, without requiring a fully valid
    config (the settings panel may still be mid-setup)."""
    data = _read_yaml_dict(path)
    return (data.get("output") or {}).get("dir", ".")


def load_forecast_form(path: str) -> dict:
    """
    Flat dict of the settings panel's editable fields (`bucksawz serve`'s
    gear icon) -- unlike `load_forecast_config`, tolerates a missing file or
    one still missing its required `actual`/`proposed` commands, since the
    panel needs to render *before* the user has finished filling them in.
    """
    data = _read_yaml_dict(path)
    return {
        "actualCommand": (data.get("actual") or {}).get("command", ""),
        "proposedCommand": (data.get("proposed") or {}).get("command", ""),
        "costExplorerCommand": (data.get("cost_explorer") or {}).get("command", ""),
        "region": data.get("region", "us-east-1"),
        "infraDir": data.get("infra_dir", ""),
    }


def save_forecast_form(path: str, form: dict) -> dict:
    """
    Merge the settings panel's edited fields into the existing YAML config
    and rewrite it, leaving any key the form doesn't know about (notably
    `output.dir`/`output.filename`) untouched. This is a whole-file rewrite
    -- comments/formatting in a hand-edited config are not preserved -- an
    accepted tradeoff once a config is administered through the panel.
    Returns the resulting form (same shape as `load_forecast_form`).
    """
    data = _read_yaml_dict(path)

    if "actualCommand" in form:
        data.setdefault("actual", {})["command"] = form["actualCommand"]
    if "proposedCommand" in form:
        data.setdefault("proposed", {})["command"] = form["proposedCommand"]
    if "costExplorerCommand" in form:
        if form["costExplorerCommand"]:
            data.setdefault("cost_explorer", {})["command"] = form["costExplorerCommand"]
        else:
            data.pop("cost_explorer", None)
    if "region" in form and form["region"]:
        data["region"] = form["region"]
    if "infraDir" in form:
        if form["infraDir"]:
            data["infra_dir"] = form["infraDir"]
        else:
            data.pop("infra_dir", None)

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.safe_dump(data, f, sort_keys=False)
    return load_forecast_form(path)
