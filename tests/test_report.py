"""Tests for the HTML report generator — exercises the JSON input path."""
import re
from pathlib import Path
import pytest
from bucksawz.schema.infracost import InfracostOutput
from bucksawz.report.render import render, _service_breakdown, _top_resources, _usage_based_items

FIXTURE = Path(__file__).parent / "fixtures" / "infracost_minimal.json"


@pytest.fixture
def output():
    return InfracostOutput.from_file(str(FIXTURE))


@pytest.fixture
def report_html(output, tmp_path):
    dest = str(tmp_path / "report.html")
    render(output, dest)
    return Path(dest).read_text()


# ── data layer ──────────────────────────────────────────────────────────────

def test_loads_two_projects(output):
    assert len(output.projects) == 2


def test_total_monthly_cost(output):
    assert output.total_monthly_cost == 150.57


def test_service_breakdown(output):
    svc = _service_breakdown(output.projects)
    assert "ELB" in svc
    assert "RDS" in svc
    assert svc["RDS"] == pytest.approx(106.58)


def test_top_resources(output):
    top = _top_resources(output.projects, n=5)
    assert top[0]["name"] == "aws_rds_cluster_instance.mysql[0]"
    assert top[0]["monthly_cost"] == pytest.approx(106.58)
    assert top[1]["name"] == "aws_lb.main"


def test_usage_based_items(output):
    items = _usage_based_items(output.projects)
    names = [i["component"] for i in items]
    assert "Load balancer capacity units" in names
    assert "Requests" in names  # SQS and Lambda


# ── rendered HTML ────────────────────────────────────────────────────────────

def test_html_has_total(report_html):
    assert "$150.57" in report_html


def test_html_has_both_projects(report_html):
    assert "stacks-network-us-east-2" in report_html
    assert "stacks-workloads-production-us-east-2" in report_html


def test_html_sidebar_toc(report_html):
    assert 'id="sidebar"' in report_html
    assert 'id="toc-list"' in report_html


def test_html_chart_data(report_html):
    # Chart.js bar chart data should contain project names
    assert "stacks-network-us-east-2" in report_html
    assert "stacks-workloads-production" in report_html


def test_html_top_resources_table(report_html):
    assert "aws_rds_cluster_instance.mysql[0]" in report_html
    assert "$106.58" in report_html


def test_html_usage_based_section(report_html):
    assert "Load balancer capacity units" in report_html
    assert "Cost depends on usage" in report_html


def test_html_resource_tags(report_html):
    assert "Env=prod" in report_html


def test_html_collapsible_sections(report_html):
    assert "<details" in report_html
    assert "<summary" in report_html


def test_html_search_input(report_html):
    assert 'id="resourceSearch"' in report_html


def test_html_chartjs_inlined(report_html):
    # Chart.js is vendored — no external CDN reference
    assert "cdn.jsdelivr.net" not in report_html
    assert "chart.js" not in report_html.lower().split("<script")[0]
    # But the Chart constructor IS present
    assert "new Chart(" in report_html


def test_html_print_css(report_html):
    assert "@media print" in report_html


def test_html_no_price_resource_not_in_top(output):
    top = _top_resources(output.projects, n=10)
    names = [r["name"] for r in top]
    assert "aws_s3_bucket.artifacts" not in names


def test_html_subresource_rendered(report_html):
    assert "root_block_device" in report_html
    assert "Storage (gp3)" in report_html


def test_html_single_account_no_account_section(report_html):
    """No account breakdown section when account_breakdown is absent (default)."""
    assert "Cost by AWS account" not in report_html
    assert "AWS accounts" not in report_html


def test_html_multi_account_section(output, tmp_path):
    """Account breakdown section rendered when multiple accounts are present."""
    dest = str(tmp_path / "multi_acct.html")
    render(output, dest, account_breakdown={
        "123456789012": 120.00,
        "234567890123": 30.57,
    })
    html = (tmp_path / "multi_acct.html").read_text()
    assert "AWS accounts (2)" in html
    assert "Cost by AWS account" in html
    assert "123456789012" in html
    assert "234567890123" in html
    # Share percentages sum to 100%
    assert "%" in html


def test_html_estimates_with_accounts(output, tmp_path):
    """estimates and account_breakdown can coexist."""
    dest = str(tmp_path / "est_acct.html")
    render(
        output, dest,
        estimates={"aws_lb.main": 24.53},
        account_breakdown={"111111111111": 120.00, "222222222222": 30.57},
    )
    html = (tmp_path / "est_acct.html").read_text()
    assert "~$24.53" in html
    assert "111111111111" in html
    assert "222222222222" in html
