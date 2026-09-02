"""
Parse `terraform show -json` output into a flat list of AWS resource configs.

Accepts either a plan (`planned_values.root_module`) or a full state
(`values.root_module`) export, since both use the same module/resource shape.

A plan also carries the pre-apply world, which `parse_prior` extracts so the
two can be priced separately and diffed. `planned_values` is the post-apply
view, so parse_state/parse_prior together give the "after" and "before".

Multi-region: a plan export also carries a `configuration` block mapping
each resource address to the provider config (and thus region) it was
planned against, via `provider_config_key` — including aliased providers
(`provider "aws" { alias = "west" }`) passed into child modules through
`module_calls[name].providers`. `_region_map_from_configuration` walks that
tree once per plan and resolves it into `{full_resource_address: region}`,
which `parse_state`/`parse_prior` attach to each `TFResource.region`. This
only resolves regions given as a literal string in the provider block
(`expressions.region.constant_value`) — a region set via variable/local
interpolation has no constant value in the plan JSON and is left
unresolved (`region=None`), falling back to `price-state`'s `--region`.
A bare `values`-only state export (no `configuration` block) can't resolve
regions at all for the same reason.
"""
from __future__ import annotations
import json
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class TFResource:
    address: str
    type: str
    name: str
    provider_name: str
    values: dict[str, Any] = field(default_factory=dict)
    region: Optional[str] = None


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


def _region_map_from_configuration(data: dict) -> dict[str, str]:
    """{full_resource_address: region} for every resource whose provider
    config resolves to a literal region string. See module docstring."""
    config = data.get("configuration") or {}
    provider_config = config.get("provider_config") or {}
    provider_regions: dict[str, str] = {}
    for key, pc in provider_config.items():
        region = ((pc.get("expressions") or {}).get("region") or {}).get("constant_value")
        if region:
            provider_regions[key] = region

    address_region: dict[str, str] = {}

    def walk(module_config: dict, address_prefix: str, key_map: dict[str, str]) -> None:
        for r in module_config.get("resources", []) or []:
            local_key = r.get("provider_config_key")
            if not local_key:
                continue
            resolved_key = key_map.get(local_key, local_key)
            region = provider_regions.get(resolved_key)
            if region:
                address_region[f"{address_prefix}{r.get('address', '')}"] = region
        for mod_name, call in (module_config.get("module_calls") or {}).items():
            child_config = call.get("module") or {}
            passed = call.get("providers") or {}
            child_key_map = {
                child_key: key_map.get(parent_key, parent_key)
                for child_key, parent_key in passed.items()
            }
            walk(child_config, f"{address_prefix}module.{mod_name}.", child_key_map)

    walk(config.get("root_module") or {}, "", {})
    return address_region


def parse_state(data: dict, region_map: Optional[dict[str, str]] = None) -> list[TFResource]:
    root = None
    if "values" in data:
        root = data["values"].get("root_module")
    elif "planned_values" in data:
        root = data["planned_values"].get("root_module")
    if root is None:
        return []
    out: list[TFResource] = []
    _walk_module(root, out)
    region_map = region_map or {}
    for r in out:
        r.region = region_map.get(r.address)
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
    region_map = _region_map_from_configuration(data)

    prior_state = data.get("prior_state")
    if prior_state:
        return parse_state(prior_state, region_map)

    out: list[TFResource] = []
    for change in data.get("resource_changes") or []:
        before = (change.get("change") or {}).get("before")
        if not before:
            continue  # null for creates
        address = change.get("address", "")
        out.append(
            TFResource(
                address=address,
                type=change.get("type", ""),
                name=change.get("name", ""),
                provider_name=change.get("provider_name", ""),
                values=before,
                region=region_map.get(address),
            )
        )
    return [r for r in out if "aws" in r.provider_name]


def parse_json(text: str) -> list[TFResource]:
    data = json.loads(text)
    return parse_state(data, _region_map_from_configuration(data))


def parse_file(path: str) -> list[TFResource]:
    with open(path) as f:
        data = json.load(f)
    return parse_state(data, _region_map_from_configuration(data))
