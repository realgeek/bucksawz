"""
Usage file: user-supplied monthly usage quantities for cost components that
can't be derived from Terraform config alone (data transfer today; the same
top-level structure could carry S3 storage GB, CloudWatch Logs ingestion,
etc. later). Infracost-usage-file-style YAML:

    data_transfer:
      internet_egress_gb_month: 10000
      inter_az_gb_month: 500

A category with no matching resource in the plan is simply unused — this
module only parses the file, `pricer.py` decides what to do with each value.
"""
from __future__ import annotations
from typing import Optional
import yaml


def load_usage_file(path: str) -> dict:
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"usage file must be a YAML mapping, got {type(data).__name__}")
    return data


def data_transfer_usage(usage: Optional[dict]) -> tuple[Optional[float], Optional[float]]:
    """Returns (internet_egress_gb_month, inter_az_gb_month) from a loaded usage file."""
    dt = (usage or {}).get("data_transfer") or {}
    egress = dt.get("internet_egress_gb_month")
    inter_az = dt.get("inter_az_gb_month")
    return (
        float(egress) if egress is not None else None,
        float(inter_az) if inter_az is not None else None,
    )
