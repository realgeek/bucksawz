"""Tests for the HTML report parser."""
import pytest
from pathlib import Path
from bucksawz.schema.html_parser import parse_html, _parse_cost

SAMPLE_HTML = """\
<!doctype html>
<html>
<head><title>Infracost cost report</title></head>
<body>
<div class="metadata">
  <ul>
    <li><span class="label">Time generated:</span><span class="value">2026-05-13 11:07:46 EDT</span></li>
  </ul>
</div>

<p class="project-name">Project: my-network</p>
<p class="project-name">Module path: stacks/network</p>
<table class="breakdown">
  <tbody>
    <tr class="resource top-level">
      <td class="name">aws_lb.main</td>
      <td class="monthly-quantity"></td>
      <td class="unit"></td>
      <td class="monthly-cost"></td>
    </tr>
    <tr class="tags">
      <td class="name">Tags: Env=prod, Team=infra</td>
      <td class="monthly-quantity"></td>
      <td class="unit"></td>
      <td class="monthly-cost"></td>
    </tr>
    <tr class="cost-component">
      <td class="name">&#8627; Application load balancer</td>
      <td class="monthly-quantity">730</td>
      <td class="unit">hours</td>
      <td class="monthly-cost">$18.40</td>
    </tr>
    <tr class="cost-component">
      <td class="name">&#8627; Load balancer capacity units</td>
      <td colspan="3" class="usage-cost">Cost depends on usage: $5.84 per LCU</td>
    </tr>
    <tr class="resource top-level">
      <td class="name">aws_instance.bastion</td>
      <td class="monthly-quantity"></td>
      <td class="unit"></td>
      <td class="monthly-cost"></td>
    </tr>
    <tr class="cost-component">
      <td class="name">&#8627; Instance usage (Linux/UNIX, on-demand, t3.micro)</td>
      <td class="monthly-quantity">730</td>
      <td class="unit">hours</td>
      <td class="monthly-cost">$7.59</td>
    </tr>
    <tr class="total">
      <td class="name" colspan="3">Project total</td>
      <td class="monthly-cost">$25.99</td>
    </tr>
  </tbody>
</table>

<table class="overall-total">
  <tbody>
    <tr class="total">
      <td class="name" colspan="3">Overall total</td>
      <td class="monthly-cost">$25.99</td>
    </tr>
  </tbody>
</table>

<div class="warnings">
  <p>5 cloud resources were detected:<br/>
  &#8729; 3 were estimated<br/>
  &#8729; 2 were free<br/>
  &#8729; 0 are not supported yet</p>
</div>
</body>
</html>
"""


@pytest.fixture
def parsed(tmp_path):
    f = tmp_path / "report.html"
    f.write_text(SAMPLE_HTML)
    return parse_html(str(f))


def test_parse_cost():
    assert _parse_cost("$1,234.56") == 1234.56
    assert _parse_cost("$0.40") == 0.40
    assert _parse_cost("") is None
    assert _parse_cost("N/A") is None


def test_time_generated(parsed):
    assert "2026-05-13" in parsed.time_generated


def test_project_count(parsed):
    assert len(parsed.projects) == 1


def test_project_name(parsed):
    assert parsed.projects[0].name == "my-network"


def test_project_module_path(parsed):
    assert parsed.projects[0].module_path() == "stacks/network"


def test_project_total(parsed):
    assert parsed.projects[0].monthly_cost() == 25.99


def test_resource_count(parsed):
    resources = parsed.projects[0].breakdown.resources
    assert len(resources) == 2


def test_resource_names(parsed):
    resources = parsed.projects[0].breakdown.resources
    assert resources[0].name == "aws_lb.main"
    assert resources[1].name == "aws_instance.bastion"


def test_resource_type_extracted(parsed):
    resources = parsed.projects[0].breakdown.resources
    assert resources[0].resource_type == "aws_lb"
    assert resources[1].resource_type == "aws_instance"


def test_resource_tags(parsed):
    lb = parsed.projects[0].breakdown.resources[0]
    assert lb.tags.get("Env") == "prod"
    assert lb.tags.get("Team") == "infra"


def test_cost_components(parsed):
    lb = parsed.projects[0].breakdown.resources[0]
    assert len(lb.cost_components) == 2
    assert lb.cost_components[0].name == "Application load balancer"
    assert lb.cost_components[0].monthly_quantity == 730.0
    assert lb.cost_components[0].monthly_cost == 18.40
    assert not lb.cost_components[0].usage_based


def test_usage_based_component(parsed):
    lb = parsed.projects[0].breakdown.resources[0]
    lcu = lb.cost_components[1]
    assert lcu.usage_based is True
    assert lcu.price == 5.84
    assert lcu.unit == "LCU"


def test_aws_service_from_parsed_name(parsed):
    lb = parsed.projects[0].breakdown.resources[0]
    assert lb.aws_service() == "ELB"
    ec2 = parsed.projects[0].breakdown.resources[1]
    assert ec2.aws_service() == "EC2"


def test_overall_total(parsed):
    assert parsed.total_monthly_cost == 25.99
