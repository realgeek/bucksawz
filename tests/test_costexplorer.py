"""Tests for the Cost Explorer data-transfer actuals fetcher (layer 3 of
data-transfer estimation: Cost Explorer supersedes --usage-file)."""
import pytest
from bucksawz.aws import costexplorer
from bucksawz.schema.infracost import InfracostOutput


class _FakePaginator:
    def __init__(self, groups_by_period):
        self._groups_by_period = groups_by_period

    def paginate(self, **kwargs):
        yield {
            "ResultsByTime": [
                {"Groups": [
                    {"Keys": [key], "Metrics": {"UsageQuantity": {"Amount": str(amount)}}}
                    for key, amount in self._groups_by_period
                ]}
            ]
        }


class _FakeCE:
    def __init__(self, groups_by_period):
        self._groups_by_period = groups_by_period

    def get_paginator(self, name):
        assert name == "get_cost_and_usage"
        return _FakePaginator(self._groups_by_period)


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch, tmp_path):
    """Route the disk cache to a throwaway directory so tests don't collide."""
    monkeypatch.setattr(costexplorer, "cache_get", lambda *a, **k: None)
    monkeypatch.setattr(costexplorer, "cache_put", lambda *a, **k: None)


def _install(monkeypatch, groups_by_period):
    monkeypatch.setattr(costexplorer, "_ce_client", lambda profile, region: _FakeCE(groups_by_period))


def test_fetch_data_transfer_actuals_splits_egress_and_inter_az(monkeypatch):
    _install(monkeypatch, [
        ("USE1-DataTransfer-Out-Bytes", 1024.0),
        ("USE1-DataTransfer-Regional-Bytes", 200.0),
    ])
    result = costexplorer.fetch_data_transfer_actuals(30, None, "us-east-1")
    assert result == {
        "internet_egress_gb_month": pytest.approx(1024.0),
        "inter_az_gb_month": pytest.approx(200.0),
    }


def test_fetch_data_transfer_actuals_excludes_inbound(monkeypatch):
    _install(monkeypatch, [
        ("USE1-DataTransfer-In-Bytes", 500.0),
    ])
    assert costexplorer.fetch_data_transfer_actuals(30, None, "us-east-1") is None


def test_fetch_data_transfer_actuals_none_without_matching_usage(monkeypatch):
    _install(monkeypatch, [
        ("USE1-BoxUsage:t3.micro", 730.0),
    ])
    assert costexplorer.fetch_data_transfer_actuals(30, None, "us-east-1") is None


def test_fetch_data_transfer_actuals_averages_over_lookback_months(monkeypatch):
    _install(monkeypatch, [
        ("USE1-DataTransfer-Out-Bytes", 6000.0),
    ])
    result = costexplorer.fetch_data_transfer_actuals(60, None, "us-east-1")
    assert result["internet_egress_gb_month"] == pytest.approx(3000.0)


# ── Account alias resolution ─────────────────────────────────────────────────


class _FakeOrgPaginator:
    def __init__(self, accounts):
        self._accounts = accounts

    def paginate(self):
        yield {"Accounts": self._accounts}


class _FakeOrg:
    def __init__(self, accounts):
        self._accounts = accounts

    def get_paginator(self, name):
        assert name == "list_accounts"
        return _FakeOrgPaginator(self._accounts)


class _DeniedOrg:
    def get_paginator(self, name):
        raise Exception("AccessDeniedException: not the management account")


def test_account_alias_map_from_organizations(monkeypatch):
    monkeypatch.setattr(
        costexplorer, "_organizations_client",
        lambda profile: _FakeOrg([
            {"Id": "111111111111", "Name": "prod"},
            {"Id": "222222222222", "Name": "staging"},
        ]),
    )
    result = costexplorer._account_alias_map(None)
    assert result == {"111111111111": "prod", "222222222222": "staging"}


def test_account_alias_map_empty_when_not_management_account(monkeypatch):
    monkeypatch.setattr(costexplorer, "_organizations_client", lambda profile: _DeniedOrg())
    assert costexplorer._account_alias_map(None) == {}


# ── Multi-region CloudWatch enrichment plumbing ─────────────────────────────


def _install_enrich_output_stubs(monkeypatch, captured):
    """Stub out every AWS call enrich_output makes except enrich_with_cloudwatch,
    whose `region` argument we capture, so we can test region plumbing without
    exercising the CE/Organizations fetch paths (covered elsewhere)."""
    monkeypatch.setattr(costexplorer, "_ce_client", lambda profile, region: object())
    monkeypatch.setattr(costexplorer, "_get_actuals_by_account_service", lambda *a, **k: {})
    monkeypatch.setattr(costexplorer, "_get_forecast", lambda *a, **k: None)
    monkeypatch.setattr(costexplorer, "_account_alias_map", lambda *a, **k: {})

    import bucksawz.aws.cloudwatch as cloudwatch_mod

    def _fake_enrich_with_cloudwatch(resources, profile, region, lookback_days, ttl_days):
        captured["region"] = region
        return {}

    monkeypatch.setattr(cloudwatch_mod, "enrich_with_cloudwatch", _fake_enrich_with_cloudwatch)


def test_enrich_output_defaults_cloudwatch_region_to_ce_region(monkeypatch):
    captured = {}
    _install_enrich_output_stubs(monkeypatch, captured)
    output = InfracostOutput(
        version="0.2", currency="USD", projects=[],
        total_hourly_cost=None, total_monthly_cost=0.0,
        time_generated="2026-01-01T00:00:00Z", summary={},
    )
    costexplorer.enrich_output(output, region="us-east-1")
    assert captured["region"] == ["us-east-1"]


def test_enrich_output_respects_explicit_cloudwatch_regions(monkeypatch):
    captured = {}
    _install_enrich_output_stubs(monkeypatch, captured)
    output = InfracostOutput(
        version="0.2", currency="USD", projects=[],
        total_hourly_cost=None, total_monthly_cost=0.0,
        time_generated="2026-01-01T00:00:00Z", summary={},
    )
    costexplorer.enrich_output(
        output, region="us-east-1", cloudwatch_regions=["us-east-1", "eu-west-1"]
    )
    assert captured["region"] == ["us-east-1", "eu-west-1"]
