"""
Parse `terraform show -json` output into a flat list of AWS resource configs.

Accepts either a plan (`planned_values.root_module`) or a full state
(`values.root_module`) export, since both use the same module/resource shape.

A plan also carries the pre-apply world, which `parse_prior` extracts so the
two can be priced separately and diffed. `planned_values` is the post-apply
view, so parse_state/parse_prior together give the "after" and "before".
"""
from __future__ import annotations
import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TFResource:
    address: str
    type: str
    name: str
    provider_name: str
    values: dict[str, Any] = field(default_factory=dict)


def _walk_module(module: dict, out: list[TFResource]) -> None:
    for r in module.get("resources", []):
        out.append(
            TFResource(
                address=r.get("address", ""),
                type=r.get("type", ""),
                name=r.get("name", ""),
                provider_name=r.get("provider_name", ""),
                values=r.get("values") or {},
            )
        )
    for child in module.get("child_modules", []):
        _walk_module(child, out)


def parse_state(data: dict) -> list[TFResource]:
    root = None
    if "values" in data:
        root = data["values"].get("root_module")
    elif "planned_values" in data:
        root = data["planned_values"].get("root_module")
    if root is None:
        return []
    out: list[TFResource] = []
    _walk_module(root, out)
    return [r for r in out if "aws" in r.provider_name]


def is_plan(data: dict) -> bool:
    """True if this export describes a proposed change rather than just a state."""
    return bool(data.get("resource_changes")) or "prior_state" in data


def parse_prior(data: dict) -> list[TFResource]:
    """
    Resource configs as they exist *before* the plan is applied.

    Prefers `prior_state`, which is a complete state export in the same shape
    parse_state already handles. Falls back to reconstructing from the `before`
    side of `resource_changes`, which some exports carry without a prior_state
    (a first apply against empty infrastructure has neither, and correctly
    yields nothing).
    """
    prior_state = data.get("prior_state")
    if prior_state:
        return parse_state(prior_state)

    out: list[TFResource] = []
    for change in data.get("resource_changes") or []:
        before = (change.get("change") or {}).get("before")
        if not before:
            continue  # null for creates
        out.append(
            TFResource(
                address=change.get("address", ""),
                type=change.get("type", ""),
                name=change.get("name", ""),
                provider_name=change.get("provider_name", ""),
                values=before,
            )
        )
    return [r for r in out if "aws" in r.provider_name]


def parse_json(text: str) -> list[TFResource]:
    return parse_state(json.loads(text))


def parse_file(path: str) -> list[TFResource]:
    with open(path) as f:
        return parse_state(json.load(f))
