"""Tests for parsing the usage file (--usage-file)."""
import pytest
from bucksawz.pricing.usage_file import data_transfer_usage, load_usage_file


def test_load_usage_file_parses_yaml(tmp_path):
    path = tmp_path / "usage.yml"
    path.write_text(
        "data_transfer:\n"
        "  internet_egress_gb_month: 10000\n"
        "  inter_az_gb_month: 500\n"
    )
    usage = load_usage_file(str(path))
    assert usage == {"data_transfer": {"internet_egress_gb_month": 10000, "inter_az_gb_month": 500}}


def test_load_usage_file_empty_file_is_empty_dict(tmp_path):
    path = tmp_path / "usage.yml"
    path.write_text("")
    assert load_usage_file(str(path)) == {}


def test_load_usage_file_rejects_non_mapping(tmp_path):
    path = tmp_path / "usage.yml"
    path.write_text("- just\n- a\n- list\n")
    with pytest.raises(ValueError):
        load_usage_file(str(path))


def test_data_transfer_usage_extracts_both_fields():
    usage = {"data_transfer": {"internet_egress_gb_month": 10000, "inter_az_gb_month": 500}}
    assert data_transfer_usage(usage) == (10000.0, 500.0)


def test_data_transfer_usage_missing_fields_are_none():
    assert data_transfer_usage({}) == (None, None)
    assert data_transfer_usage(None) == (None, None)
    assert data_transfer_usage({"data_transfer": {"internet_egress_gb_month": 500}}) == (500.0, None)
