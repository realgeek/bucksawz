"""Tests for the local disk cache."""
import time
import pytest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from bucksawz.aws.cache import get, put, invalidate, clear_expired, cache_key


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr("bucksawz.aws.cache._CACHE_DIR", tmp_path / "cache")


def test_cache_miss():
    assert get("nonexistent") is None


def test_cache_put_and_get():
    put("mykey", {"value": 42})
    result = get("mykey")
    assert result == {"value": 42}


def test_cache_expired():
    put("oldkey", "stale")
    # Patch datetime to simulate expiry
    future = datetime.now(timezone.utc) + timedelta(days=8)
    with patch("bucksawz.aws.cache.datetime") as mock_dt:
        mock_dt.now.return_value = future
        mock_dt.fromisoformat = datetime.fromisoformat
        assert get("oldkey", ttl_days=7) is None


def test_cache_not_expired():
    put("freshkey", "fresh")
    future = datetime.now(timezone.utc) + timedelta(days=3)
    with patch("bucksawz.aws.cache.datetime") as mock_dt:
        mock_dt.now.return_value = future
        mock_dt.fromisoformat = datetime.fromisoformat
        assert get("freshkey", ttl_days=7) == "fresh"


def test_invalidate():
    put("todelete", 123)
    assert get("todelete") == 123
    invalidate("todelete")
    assert get("todelete") is None


def test_clear_expired_removes_old(tmp_path, monkeypatch):
    monkeypatch.setattr("bucksawz.aws.cache._CACHE_DIR", tmp_path / "cache")
    put("old", "x")
    put("new", "y")

    future = datetime.now(timezone.utc) + timedelta(days=8)
    with patch("bucksawz.aws.cache.datetime") as mock_dt:
        mock_dt.now.return_value = future
        mock_dt.fromisoformat = datetime.fromisoformat
        removed = clear_expired(ttl_days=7)

    assert removed == 2  # both are old from future's perspective
    assert get("old") is None
    assert get("new") is None


def test_cache_key():
    k = cache_key("ce_actuals", "default", "us-east-1", "2026-01-01", "2026-04-01")
    assert "ce_actuals" in k
    assert ":" in k


def test_cache_stores_various_types():
    put("list", [1, 2, 3])
    put("dict", {"a": 1.5})
    put("num", 3.14)
    assert get("list") == [1, 2, 3]
    assert get("dict") == {"a": 1.5}
    assert get("num") == 3.14
