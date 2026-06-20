"""
HTML report renderer. Produces a single self-contained HTML file.
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Optional
from jinja2 import Environment, FileSystemLoader, select_autoescape
from ..schema.infracost import InfracostOutput, Project, Resource, CostComponent

_ASSETS = Path(__file__).parent / "assets"


def _fmt_cost(value: Optional[float], currency: str = "USD") -> str:
    if value is None:
        return ""
    symbol = "$" if currency == "USD" else currency + " "
    if value >= 1000:
        return f"{symbol}{value:,.2f}"
    return f"{symbol}{value:.2f}"


def _service_breakdown(projects: list[Project]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for p in projects:
        if not p.breakdown:
            continue
        for r in p.breakdown.resources:
            svc = r.aws_service()
            cost = r.total_monthly_cost()
            totals[svc] = totals.get(svc, 0.0) + cost
    return dict(sorted(totals.items(), key=lambda x: -x[1]))


def _top_resources(projects: list[Project], n: int = 10) -> list[dict]:
    items = []
    for p in projects:
        if not p.breakdown:
            continue
        for r in p.breakdown.resources:
            cost = r.total_monthly_cost()
            if cost > 0:
                items.append({
                    "project": p.name,
                    "name": r.name,
                    "resource_type": r.resource_type,
                    "monthly_cost": cost,
                })
    items.sort(key=lambda x: -x["monthly_cost"])
    return items[:n]


def _usage_based_items(projects: list[Project]) -> list[dict]:
    items = []
    for p in projects:
        if not p.breakdown:
            continue
        for r in p.breakdown.resources:
            for c in r.cost_components:
                if c.usage_based and c.price is not None:
                    items.append({
                        "project": p.name,
                        "resource": r.name,
                        "component": c.name,
                        "unit": c.unit,
                        "price": c.price,
                    })
            for sub in r.sub_resources:
                for c in sub.cost_components:
                    if c.usage_based and c.price is not None:
                        items.append({
                            "project": p.name,
                            "resource": f"{r.name} / {sub.name}",
                            "component": c.name,
                            "unit": c.unit,
                            "price": c.price,
                        })
    return items


def _unsupported_resources(projects: list[Project]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for p in projects:
        summary = p.summary
        for item in summary.get("unsupportedResourceCounts") or {}:
            rt = item if isinstance(item, str) else list(item.keys())[0]
            cnt = 1 if isinstance(item, str) else list(item.values())[0]
            counts[rt] = counts.get(rt, 0) + cnt
    return dict(sorted(counts.items(), key=lambda x: -x[1]))


def _resource_rows(resource: Resource, depth: int = 0) -> list[dict]:
    """Flatten a resource tree into table rows."""
    rows = []
    indent = "&nbsp;" * (depth * 4)
    arrow = "&#8627; " if depth > 0 else ""
    cost_str = _fmt_cost(resource.total_monthly_cost() or None)

    rows.append({
        "kind": "resource",
        "top_level": depth == 0,
        "name": f"{indent}{arrow}{resource.name}",
        "resource_type": resource.resource_type,
        "tags": resource.tags,
        "monthly_cost": cost_str,
        "depth": depth,
    })

    for c in resource.cost_components:
        if c.usage_based:
            cost_cell = f"Cost depends on usage: {_fmt_cost(c.price)} per {c.unit}"
            rows.append({
                "kind": "component",
                "usage_based": True,
                "name": f"{indent}&nbsp;&nbsp;&nbsp;&nbsp;&#8627; {c.name}",
                "monthly_qty": "",
                "unit": "",
                "monthly_cost": cost_cell,
            })
        else:
            rows.append({
                "kind": "component",
                "usage_based": False,
                "name": f"{indent}&nbsp;&nbsp;&nbsp;&nbsp;&#8627; {c.name}",
                "monthly_qty": f"{c.monthly_quantity:,.0f}" if c.monthly_quantity is not None else "",
                "unit": c.unit,
                "monthly_cost": _fmt_cost(c.monthly_cost),
            })

    for sub in resource.sub_resources:
        rows.extend(_resource_rows(sub, depth + 1))

    return rows


def render(output: InfracostOutput, dest: str) -> None:
    env = Environment(
        loader=FileSystemLoader(str(Path(__file__).parent)),
        autoescape=select_autoescape(["html"]),
    )
    env.filters["fmt_cost"] = lambda v: _fmt_cost(v, output.currency)

    svc_breakdown = _service_breakdown(output.projects)
    top_resources = _top_resources(output.projects)
    usage_based = _usage_based_items(output.projects)

    projects_data = []
    for p in output.projects:
        if not p.breakdown:
            continue
        rows = []
        for r in p.breakdown.resources:
            rows.extend(_resource_rows(r))
        projects_data.append({
            "name": p.name,
            "module_path": p.module_path(),
            "monthly_cost": p.monthly_cost(),
            "monthly_cost_fmt": _fmt_cost(p.monthly_cost(), output.currency),
            "rows": rows,
            "anchor": p.name.replace("/", "-").replace(" ", "-"),
        })

    chartjs = (_ASSETS / "chart.min.js").read_text(encoding="utf-8")

    tpl = env.get_template("template.html")
    html = tpl.render(
        output=output,
        currency=output.currency,
        total_monthly_cost=_fmt_cost(output.total_monthly_cost, output.currency),
        projects=projects_data,
        svc_breakdown=svc_breakdown,
        svc_breakdown_json=json.dumps(svc_breakdown),
        top_resources=top_resources,
        usage_based=usage_based,
        project_costs_json=json.dumps({
            p["name"]: p["monthly_cost"] for p in projects_data
        }),
        chartjs=chartjs,
        fmt_cost=_fmt_cost,
    )

    Path(dest).write_text(html, encoding="utf-8")
    print(f"Report written to {dest}")
