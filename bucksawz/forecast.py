"""
`bucksawz forecast`: price two independently-generated terraform datasets --
what's actually deployed today, and what the whole project would look like
fully deployed (e.g. with `not-ready`-tagged resources included) -- and
write one timestamped JSON file plus a small manifest that the static
viewer (`bucksawz serve`) reads to always show the latest run. See
forecast_config.py for the YAML schema.

This deliberately doesn't also write a full HTML report per run: only the
data changes between runs, so the viewer (bucksawz_viewer.html) is written
once and just re-fetches whichever JSON file the manifest currently points
to.
"""
from __future__ import annotations
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .forecast_config import ForecastConfig, VIEWER_FILENAME
from .pricing.pricer import extrapolate_by_resource_type, find_new_resources, price_terraform_json


def run_command(command: str) -> dict:
    """
    Run a shell command and parse its stdout as JSON. Runs through the
    user's own shell (not shlex.split) since these commands are commonly
    wrapped in aws-vault/direnv/pipes -- the config file is trusted,
    user-authored input, the same trust level as a Makefile target.
    """
    result = subprocess.run(command, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"command failed ({result.returncode}): {command}\n{result.stderr}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"command did not produce valid JSON: {command}\n{e}") from e


def build_forecast_payload(actual_data: dict, proposed_data: dict, region: str) -> dict:
    """Pure function -- no I/O -- so it's the part worth unit testing directly."""
    actual = price_terraform_json(actual_data, region)
    proposed = price_terraform_json(proposed_data, region)

    new_by_project = find_new_resources(actual, proposed)
    extrapolated = extrapolate_by_resource_type(actual, new_by_project)

    new_resources = [
        {
            "project": project,
            "name": r.name,
            "resourceType": r.resource_type,
            "extrapolatedMonthlyCost": extrapolated.get(project, {}).get(r.name),
        }
        for project, resources in new_by_project.items()
        for r in resources
    ]

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "region": region,
        "actual": actual.to_dict(),
        "proposed": proposed.to_dict(),
        "newResources": new_resources,
    }


def ensure_viewer(output_dir: Path) -> Path:
    """
    (Re)write the static viewer.html. It's cheap boilerplate with no
    run-specific data baked in, so overwriting it every run is safe and
    keeps it in sync with whatever bucksawz version generated it.
    """
    from importlib import resources
    viewer_path = Path(output_dir) / VIEWER_FILENAME
    template = resources.files("bucksawz").joinpath("forecast_viewer.html").read_text()
    viewer_path.write_text(template)
    return viewer_path


def run_forecast(config: ForecastConfig) -> Path:
    """Run both configured commands, price and compare the results, and
    write the timestamped JSON + manifest + viewer into config.output_dir.
    Returns the path of the JSON file written."""
    actual_data = run_command(config.actual_command)
    proposed_data = run_command(config.proposed_command)
    payload = build_forecast_payload(actual_data, proposed_data, config.region)

    output_dir = Path(config.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    output_path = config.output_path()
    with open(output_path, "w") as f:
        json.dump(payload, f, indent=2, default=str)

    with open(config.manifest_path(), "w") as f:
        json.dump({"latest": output_path.name}, f, indent=2)

    ensure_viewer(output_dir)
    return output_path
