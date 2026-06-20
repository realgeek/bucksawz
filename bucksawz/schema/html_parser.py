"""
Parse an infracost HTML report back into an InfracostOutput object.
This is useful for testing and for re-generating improved reports from
existing HTML output when the original JSON is unavailable.
"""
from __future__ import annotations
import re
from html.parser import HTMLParser
from typing import Optional
from .infracost import (
    InfracostOutput, Project, Breakdown, Resource, CostComponent,
)


def _parse_cost(s: str) -> Optional[float]:
    """'$1,234.56' → 1234.56"""
    s = s.strip().lstrip("$").replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


class _HtmlReportParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self._stack: list[str] = []
        self._text_buf = ""

        # state
        self.time_generated: str = ""
        self.projects: list[dict] = []

        self._cur_proj: Optional[dict] = None
        self._cur_resource: Optional[dict] = None
        self._cur_subresource: Optional[dict] = None
        self._cur_component: Optional[dict] = None
        self._in_label = False
        self._in_value = False
        self._label_text = ""

        # tr class tracking
        self._cur_tr_class = ""
        self._cur_td_class = ""
        self._td_index = 0

    # ── helpers ──────────────────────────────────────────────────────────
    def _push(self, tag): self._stack.append(tag)
    def _pop(self):
        if self._stack: self._stack.pop()
    def _in(self, tag): return tag in self._stack

    # ── SAX callbacks ─────────────────────────────────────────────────────
    def handle_starttag(self, tag, attrs):
        attrs_d = dict(attrs)
        cls = attrs_d.get("class", "")
        self._push(tag)
        self._text_buf = ""

        if tag == "p" and "project-name" in cls:
            self._cur_tag_class = "project-name"
        elif tag == "tr":
            self._cur_tr_class = cls
            self._td_index = 0
        elif tag == "td":
            self._cur_td_class = attrs_d.get("class", "")
        elif tag == "span":
            if "label" in cls:
                self._in_label = True
            elif "value" in cls:
                self._in_value = True

    def handle_endtag(self, tag):
        text = self._text_buf.strip()

        if tag == "p" and getattr(self, "_cur_tag_class", "") == "project-name":
            self._handle_project_name_p(text)
            self._cur_tag_class = ""

        elif tag == "span":
            if self._in_label:
                self._label_text = text
                self._in_label = False
            elif self._in_value:
                self._handle_meta_value(self._label_text, text)
                self._in_value = False

        elif tag == "td":
            self._handle_td(text)
            self._td_index += 1

        elif tag == "tr":
            self._finish_tr()

        self._pop()
        self._text_buf = ""

    def handle_data(self, data):
        self._text_buf += data

    def handle_entityref(self, name):
        _ents = {"amp": "&", "lt": "<", "gt": ">", "nbsp": " ", "8627": "↳"}
        self._text_buf += _ents.get(name, "")

    def handle_charref(self, name):
        try:
            self._text_buf += chr(int(name))
        except Exception:
            pass

    # ── logic ─────────────────────────────────────────────────────────────
    def _handle_meta_value(self, label: str, value: str):
        label = label.strip().rstrip(":").lower()
        if "time generated" in label:
            self.time_generated = value

    def _handle_project_name_p(self, text: str):
        if text.startswith("Project:"):
            name = text.replace("Project:", "").strip()
            self._cur_proj = {
                "name": name,
                "metadata": {},
                "resources": [],
            }
            self.projects.append(self._cur_proj)
            self._cur_resource = None
        elif text.startswith("Module path:"):
            if self._cur_proj:
                self._cur_proj["metadata"]["path"] = text.replace("Module path:", "").strip()

    def _handle_td(self, text: str):
        cls = self._cur_td_class
        tr_cls = self._cur_tr_class

        if "top-level" in tr_cls:
            if "name" in cls and self._td_index == 0:
                resource_name = re.sub(r"\s+", " ", text).strip()
                rt = resource_name.split(".")[0].strip() if "." in resource_name else ""
                self._cur_resource = {
                    "name": resource_name,
                    "resourceType": rt,
                    "tags": {},
                    "costComponents": [],
                    "subresources": [],
                }
                if self._cur_proj:
                    self._cur_proj["resources"].append(self._cur_resource)
                self._cur_subresource = None
            elif "monthly-cost" in cls and self._td_index == 3:
                if self._cur_resource:
                    self._cur_resource["monthlyCost"] = _parse_cost(text)

        elif "resource" in tr_cls and "top-level" not in tr_cls:
            if "name" in cls and self._td_index == 0:
                sub_name = re.sub(r"[\s↳]+", " ", text).strip()
                self._cur_subresource = {
                    "name": sub_name,
                    "resourceType": "",
                    "tags": {},
                    "costComponents": [],
                    "subresources": [],
                }
                if self._cur_resource:
                    self._cur_resource["subresources"].append(self._cur_subresource)

        elif "cost-component" in tr_cls:
            if "name" in cls and self._td_index == 0:
                comp_name = re.sub(r"[\s↳]+", " ", text).strip()
                self._cur_component = {
                    "name": comp_name,
                    "unit": "",
                    "monthlyQuantity": None,
                    "monthlyCost": None,
                    "price": None,
                    "usageBased": False,
                }
                target = self._cur_subresource or self._cur_resource
                if target:
                    target["costComponents"].append(self._cur_component)
            elif "monthly-quantity" in cls and self._td_index == 1:
                if self._cur_component and text:
                    self._cur_component["monthlyQuantity"] = _parse_cost(text)
            elif "unit" in cls and self._td_index == 2:
                if self._cur_component:
                    self._cur_component["unit"] = text
            elif "monthly-cost" in cls and self._td_index == 3:
                if self._cur_component:
                    self._cur_component["monthlyCost"] = _parse_cost(text)
            elif "usage-cost" in cls:
                # "Cost depends on usage: $0.40 per 1M queries"
                if self._cur_component:
                    self._cur_component["usageBased"] = True
                    m = re.search(r"\$([\d.]+)\s+per\s+(.+)", text)
                    if m:
                        self._cur_component["price"] = float(m.group(1))
                        self._cur_component["unit"] = m.group(2).strip()

        elif "total" in tr_cls and "monthly-cost" in cls:
            cost = _parse_cost(text)
            if cost is not None and self._cur_proj:
                self._cur_proj["projectTotal"] = cost

        elif "tags" in tr_cls and self._td_index == 0:
            # "Tags: Company=MPC, Environment=network"
            m = re.search(r"Tags:\s*(.+)", text.replace("\n", " "))
            if m and self._cur_resource:
                for pair in m.group(1).split(","):
                    pair = pair.strip()
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        self._cur_resource["tags"][k.strip()] = v.strip()

    def _finish_tr(self):
        self._cur_tr_class = ""
        self._td_index = 0


def parse_html(path: str) -> InfracostOutput:
    """Parse an infracost HTML report file into an InfracostOutput object."""
    with open(path, encoding="utf-8") as f:
        html = f.read()

    # Extract overall summary from warnings section
    total_match = re.search(r"Overall total</td>\s*<td[^>]*>\$([\d,]+\.?\d*)</td>", html)
    total_monthly = None
    if total_match:
        total_monthly = float(total_match.group(1).replace(",", ""))

    # Extract resource counts from warnings paragraph
    detected = estimated = free = unsupported = 0
    counts_match = re.search(
        r"(\d+) cloud resources were detected.*?(\d+) were estimated.*?(\d+) were free.*?(\d+) are not supported",
        html, re.DOTALL
    )
    if counts_match:
        detected  = int(counts_match.group(1))
        estimated = int(counts_match.group(2))
        free      = int(counts_match.group(3))
        unsupported = int(counts_match.group(4))

    # Extract unsupported resource type counts
    unsupported_counts = {}
    for m in re.finditer(r"(\d+)\s+x\s+(aws_\w+)", html):
        unsupported_counts[m.group(2)] = int(m.group(1))

    # Extract time generated
    time_match = re.search(r"Time generated.*?<span[^>]*>(.*?)</span>", html, re.DOTALL)
    time_generated = time_match.group(1).strip() if time_match else ""

    parser = _HtmlReportParser()
    parser.feed(html)

    projects = []
    for pd in parser.projects:
        resources = []
        for rd in pd.get("resources", []):
            resource = Resource(
                name=rd["name"],
                resource_type=rd.get("resourceType", ""),
                tags=rd.get("tags", {}),
                monthly_cost=rd.get("monthlyCost"),
                hourly_cost=None,
                cost_components=[
                    CostComponent(
                        name=c["name"],
                        unit=c.get("unit", ""),
                        hourly_quantity=None,
                        monthly_quantity=c.get("monthlyQuantity"),
                        price=c.get("price"),
                        hourly_cost=None,
                        monthly_cost=c.get("monthlyCost"),
                        usage_based=c.get("usageBased", False),
                    )
                    for c in rd.get("costComponents", [])
                ],
                sub_resources=[
                    Resource(
                        name=s["name"],
                        resource_type="",
                        tags={},
                        monthly_cost=None,
                        hourly_cost=None,
                        cost_components=[
                            CostComponent(
                                name=c["name"],
                                unit=c.get("unit", ""),
                                hourly_quantity=None,
                                monthly_quantity=c.get("monthlyQuantity"),
                                price=c.get("price"),
                                hourly_cost=None,
                                monthly_cost=c.get("monthlyCost"),
                                usage_based=c.get("usageBased", False),
                            )
                            for c in s.get("costComponents", [])
                        ],
                        sub_resources=[],
                    )
                    for s in rd.get("subresources", [])
                ],
            )
            resources.append(resource)

        proj_total = pd.get("projectTotal")

        breakdown = Breakdown(
            resources=resources,
            total_hourly_cost=None,
            total_monthly_cost=proj_total,
        )
        project = Project(
            name=pd["name"],
            metadata=pd.get("metadata", {}),
            past_breakdown=None,
            breakdown=breakdown,
            diff=None,
            summary={},
        )
        projects.append(project)

    return InfracostOutput(
        version="0.2",
        currency="USD",
        projects=projects,
        total_hourly_cost=None,
        total_monthly_cost=total_monthly,
        time_generated=time_generated,
        summary={
            "totalDetectedResources": detected,
            "totalSupportedResources": estimated,
            "totalNoPriceResources": free,
            "totalUnsupportedResources": unsupported,
            "unsupportedResourceCounts": unsupported_counts,
        },
    )
