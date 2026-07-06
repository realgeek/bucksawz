"""
Parse `terraform show -json` output into a flat list of AWS resource configs.

Accepts either a plan (`planned_values.root_module`) or a full state
(`values.root_module`) export, since both use the same module/resource shape.
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


def parse_json(text: str) -> list[TFResource]:
    return parse_state(json.loads(text))


def parse_file(path: str) -> list[TFResource]:
    with open(path) as f:
        return parse_state(json.load(f))
