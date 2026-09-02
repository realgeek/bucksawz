"""Tests for the Cost Explorer data-transfer actuals fetcher (layer 3 of
data-transfer estimation: Cost Explorer supersedes --usage-file)."""
import pytest
from bucksawz.aws import costexplorer


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
