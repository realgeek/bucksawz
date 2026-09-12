"""CLI-level regression test for `price-state`'s multi-stack data-transfer wiring."""
import json
import textwrap

from click.testing import CliRunner

from bucksawz.cli import cli
from bucksawz.pricing import db as price_db


def test_price_state_multi_stack_includes_data_transfer_estimate(tmp_path, monkeypatch):
    """Regression: the raw_multi_stack/raw_flat branch of price-state used to
    skip price_data_transfer/estimate_data_transfer_cost entirely, so a
    combined multi-stack report never showed any network/egress cost even
    with --usage-file supplied."""
    db_path = tmp_path / "prices.db"
    price_db.upsert("AWSDataTransfer", "us-east-1", "datatransfer:out:0", "GB", 0.09, db=db_path)
    price_db.upsert("AWSDataTransfer", "us-east-1", "datatransfer:regional", "GB", 0.01, db=db_path)
    # `_DEFAULT_DB` is resolved once at import time from BUCKSAWZ_PRICE_DB, and
    # cli.py's price-state has no --db-path option, so patch the module constant
    # directly rather than the env var.
    monkeypatch.setattr(price_db, "_DEFAULT_DB", db_path)

    input_path = tmp_path / "combined.json"
    input_path.write_text(json.dumps({"stack-a": {"resources": []}, "stack-b": {"resources": []}}))

    usage_path = tmp_path / "usage.yml"
    usage_path.write_text(textwrap.dedent("""
        data_transfer:
          internet_egress_gb_month: 5000
          inter_az_gb_month: 100
    """))

    output_html = tmp_path / "report.html"
    output_json = tmp_path / "report.json"

    result = CliRunner().invoke(cli, [
        "price-state",
        "--input", str(input_path),
        "--output", str(output_html),
        "--usage-file", str(usage_path),
        "--json-output", str(output_json),
    ])
    assert result.exit_code == 0, result.output

    written = json.loads(output_json.read_text())
    stack_a_resources = written["projects"][0]["breakdown"]["resources"]
    assert any(r["name"] == "Data Transfer (us-east-1)" for r in stack_a_resources)

    assert "~$451.00" in output_html.read_text()
