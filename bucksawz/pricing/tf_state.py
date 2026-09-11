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


def _index_suffix(index_key: Any) -> str:
    if index_key is None:
        return ""
    if isinstance(index_key, str):
        return f'["{index_key}"]'
    return f"[{index_key}]"


def parse_raw_state(data: dict) -> list[TFResource]:
    """
    Parse a raw `terraform.tfstate` export (e.g. from `terraform state pull`),
    as opposed to `terraform show -json`. Top level is `resources: [{module,
    mode, type, name, provider, instances: [{index_key, attributes}]}]` —
    structurally different from show-json's `values.root_module.resources`
    (attributes live under each instance, not a single `values` dict, and
    there's no pre-resolved `address`). Multi-instance resources
    (`count`/`for_each`) are split into one TFResource per instance, address
    reconstructed the way Terraform itself would render it.

    Newer AWS provider versions accept a per-resource `region` argument;
    when a resource sets it, it lands in `attributes.region` and is used
    directly here — this is often the only region signal available at all,
    since a raw state file (unlike a plan) carries no `configuration` block
    to resolve provider aliases from.
    """
    out: list[TFResource] = []
    for r in data.get("resources") or []:
        if r.get("mode") not in (None, "managed"):
            continue
        rtype = r.get("type", "")
        name = r.get("name", "")
        module = r.get("module")
        provider = r.get("provider", "")
        prefix = f"{module}." if module else ""
        for instance in r.get("instances") or []:
            suffix = _index_suffix(instance.get("index_key"))
            attrs = instance.get("attributes") or {}
            out.append(
                TFResource(
                    address=f"{prefix}{rtype}.{name}{suffix}",
                    type=rtype,
                    name=name,
                    provider_name=provider,
                    values=attrs,
                    region=attrs.get("region"),
                )
            )
    return [r for r in out if "aws" in r.provider_name]


def parse_raw_multi_stack(data: dict[str, dict]) -> dict[str, list[TFResource]]:
    """combined.json shape: {stack_path: <raw tfstate dict>, ...}."""
    return {stack: parse_raw_state(state) for stack, state in data.items()}


def parse_raw_flat(entries: list[dict]) -> dict[str, list[TFResource]]:
    """combined_flat.json shape: a flat list of raw-state resource blocks,
    each tagged with a `_stack` key naming which stack it came from."""
    grouped: dict[str, list[dict]] = {}
    for entry in entries:
        grouped.setdefault(entry.get("_stack", ""), []).append(entry)
    return {stack: parse_raw_state({"resources": resources}) for stack, resources in grouped.items()}


def detect_format(data: Any) -> str:
    """
    One of: "show_json" (plan/state via `terraform show -json`), "raw_state"
    (a single raw tfstate export), "raw_multi_stack" (combined.json: a dict
    of stack path -> raw tfstate), "raw_flat" (combined_flat.json: a flat
    list of raw-state resource blocks tagged with `_stack`), or "unknown".
    """
    if isinstance(data, list):
        return "raw_flat"
    if isinstance(data, dict):
        if "values" in data or "planned_values" in data or "resource_changes" in data:
            return "show_json"
        if "resources" in data and isinstance(data.get("resources"), list):
            return "raw_state"
        if data and all(isinstance(v, dict) and "resources" in v for v in data.values()):
            return "raw_multi_stack"
    return "unknown"


def parse_json(text: str) -> list[TFResource]:
    data = json.loads(text)
    return parse_state(data, _region_map_from_configuration(data))


def parse_file(path: str) -> list[TFResource]:
    with open(path) as f:
        data = json.load(f)
    return parse_state(data, _region_map_from_configuration(data))
