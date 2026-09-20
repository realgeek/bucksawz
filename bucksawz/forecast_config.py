"""
Config file for `bucksawz forecast`: shell commands that produce the
"actual" (currently deployed) and "proposed" (fully deployed, e.g. with
`not-ready`-tagged resources included) terraform state/plan JSON, plus
where to write the priced comparison output. Both `bucksawz forecast` and
`bucksawz serve` default `--config` to `~/.bucksawz/config.yml` (or
`config.yaml`, if that's the one that exists -- see `resolve_config_path`);
`save_forecast_form` creates that directory if it's missing. Per-repo
settings live in named `repos:` sections of that one file. Pass `--config`
explicitly to use a different path.

Several infra repos can share one config: put settings common to all of them
at the top level and each repo's own settings in a named subsection under
`repos:`. A repo's values override the common ones (mapping sections like
`output`/`cost_explorer` merge key by key), and each repo writes into
`<output.dir>/<repo name>/` unless it sets its own `output.dir`:

    region: us-east-1
    output:
      dir: reports
    repos:
      network:
        infra_dir: ~/infra/network
        actual:   {command: "tofu show -json"}
        proposed: {command: "./plan_all.sh"}
      apps:
        infra_dir: ~/infra/apps
        region: eu-west-1
        actual:   {command: "tofu show -json"}
        proposed: {command: "./plan_all.sh"}

One repo is worked on at a time, chosen with `--repo NAME` (the name is an
alias -- e.g. `workiac` for ~/code/work/infrastructure -- pointed at its
directory by `infra_dir`). `--repo` is required whenever the file has a
`repos:` section. A file with no `repos:` section is the flat single-repo
shape below. YAML:

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
import os
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


def resolve_config_path(explicit: Optional[str] = None) -> str:
    """`--config` if given, else ~/.bucksawz/config.yml or config.yaml
    (whichever exists, .yml preferred; .yml is what gets created if neither)."""
    if explicit:
        return explicit
    base = Path.home() / ".bucksawz"
    for name in ("config.yml", "config.yaml"):
        if (base / name).exists():
            return str(base / name)
    return str(base / "config.yml")


def _merge_section(common: dict, repo: dict) -> dict:
    """Repo settings win over common ones. One-level deep merge for mapping
    values (so a repo can override just `output.dir` or `cost_explorer.command`
    without restating the rest of the section); anything else is replaced."""
    merged = dict(common)
    for key, value in repo.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def _build_config(data: dict) -> ForecastConfig:
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
        infra_dir=os.path.expanduser(data["infra_dir"]) if data.get("infra_dir") else None,
    )


def _resolve(data: dict, repo: Optional[str]) -> dict:
    """
    Flatten a parsed config into the single-repo shape `_build_config`
    reads. Without a `repos:` section the file is the legacy flat
    single-repo shape and `repo` must be None. With one, `repo` is
    required -- there is no implicit default -- and that entry is merged
    over the common top-level settings.
    """
    repos = data.get("repos")
    if repos is None:
        if repo is not None:
            raise ValueError(f"forecast config has no 'repos' section, can't select repo '{repo}'")
        return data
    if not isinstance(repos, dict) or not repos:
        raise ValueError("forecast config 'repos' must be a non-empty mapping of name -> settings")
    if repo is None:
        raise ValueError(f"forecast config defines repos ({', '.join(repos)}); choose one with --repo")
    if repo not in repos:
        raise ValueError(f"unknown repo '{repo}' (defined: {', '.join(repos)})")

    common = {k: v for k, v in data.items() if k != "repos"}
    entry = repos[repo] or {}
    merged = _merge_section(common, entry)
    # Each repo gets its own output subdirectory unless it names one, so runs
    # (and their manifests/viewers) don't overwrite each other.
    if "dir" not in (entry.get("output") or {}):
        base = (common.get("output") or {}).get("dir", ".")
        merged["output"] = {**(merged.get("output") or {}), "dir": str(Path(base) / repo)}
    return merged


def load_forecast_config(path: str, repo: Optional[str] = None) -> ForecastConfig:
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"forecast config must be a YAML mapping, got {type(data).__name__}")
    return _build_config(_resolve(data, repo))


def _read_yaml_dict(path: str) -> dict:
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        data = {}
    return data if isinstance(data, dict) else {}


def load_output_dir(path: str, repo: Optional[str] = None) -> str:
    """The configured output.dir (default '.'), tolerating a missing file --
    used by `bucksawz serve --dir`'s default so it matches wherever
    `bucksawz forecast` actually writes, without requiring a fully valid
    config (the settings panel may still be mid-setup)."""
    data = _read_yaml_dict(path)
    if repo is None:
        if data.get("repos"):
            _resolve(data, None)  # raises: --repo is required
        return (data.get("output") or {}).get("dir", ".")
    # An alias not saved yet (no `repos:` section, or no such entry) is
    # tolerated -- the settings panel creates it on first save -- and just
    # resolves to the default <output.dir>/<repo> location.
    entry = (data.get("repos") or {}).get(repo) or {}
    if "dir" in (entry.get("output") or {}):
        return entry["output"]["dir"]
    return str(Path((data.get("output") or {}).get("dir", ".")) / repo)


def _form_view(data: dict, repo: Optional[str]) -> dict:
    """The settings the panel should show: with a repo, that repo's entry
    merged over the common top-level settings (an unknown repo is tolerated --
    it may not have been saved yet -- and just shows the common values)."""
    if repo is None:
        return data
    common = {k: v for k, v in data.items() if k != "repos"}
    entry = (data.get("repos") or {}).get(repo) or {}
    return _merge_section(common, entry)


def load_forecast_form(path: str, repo: Optional[str] = None) -> dict:
    """
    Flat dict of the settings panel's editable fields (`bucksawz serve`'s
    gear icon) -- unlike `load_forecast_config`, tolerates a missing file or
    one still missing its required `actual`/`proposed` commands, since the
    panel needs to render *before* the user has finished filling them in.

    With `repo`, shows that repo's effective values (its entry over the
    common settings), matching what `bucksawz forecast --repo` would run;
    `"repo"` echoes the name so the panel can say where saves go.
    """
    data = _form_view(_read_yaml_dict(path), repo)
    return {
        "repo": repo or "",
        "actualCommand": (data.get("actual") or {}).get("command", ""),
        "proposedCommand": (data.get("proposed") or {}).get("command", ""),
        "costExplorerCommand": (data.get("cost_explorer") or {}).get("command", ""),
        "region": data.get("region", "us-east-1"),
        "infraDir": data.get("infra_dir", ""),
    }


def save_forecast_form(path: str, form: dict, repo: Optional[str] = None) -> dict:
    """
    Merge the settings panel's edited fields into the existing YAML config
    and rewrite it, leaving any key the form doesn't know about (notably
    `output.dir`/`output.filename`) untouched. With `repo`, edits go into
    that repo's `repos.<repo>` subsection (created if needed) rather than
    the common top level; an emptied optional field is removed from the
    repo entry, so the common value (if any) shows through again. This is a
    whole-file rewrite -- comments/formatting in a hand-edited config are
    not preserved -- an accepted tradeoff once a config is administered
    through the panel. Returns the resulting form (same shape as
    `load_forecast_form`).
    """
    root = _read_yaml_dict(path)
    if repo is None:
        data = root
    else:
        repos = root.get("repos")
        if not isinstance(repos, dict):
            repos = root["repos"] = {}
        if not isinstance(repos.get(repo), dict):
            repos[repo] = {}
        data = repos[repo]

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
        yaml.safe_dump(root, f, sort_keys=False)
    return load_forecast_form(path, repo)
