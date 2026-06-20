"""
Local disk cache for AWS pricing and Cost Explorer results.
Default TTL: 7 days. Cache stored in ~/.cache/bucksawz/.
"""
from __future__ import annotations
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

_DEFAULT_TTL_DAYS = 7
_CACHE_DIR = Path(os.environ.get("BUCKSAWZ_CACHE_DIR", Path.home() / ".cache" / "bucksawz"))


def _cache_path(key: str) -> Path:
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    return _CACHE_DIR / f"{digest}.json"


def get(key: str, ttl_days: int = _DEFAULT_TTL_DAYS) -> Optional[Any]:
    """Return cached value if present and not expired, else None."""
    path = _cache_path(key)
    if not path.exists():
        return None
    try:
        envelope = json.loads(path.read_text())
        cached_at = datetime.fromisoformat(envelope["cached_at"])
        if datetime.now(timezone.utc) - cached_at > timedelta(days=ttl_days):
            path.unlink(missing_ok=True)
            return None
        return envelope["data"]
    except Exception:
        return None


def put(key: str, data: Any) -> None:
    """Write data to cache with current timestamp."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    envelope = {
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "key": key,
        "data": data,
    }
    _cache_path(key).write_text(json.dumps(envelope, default=str))


def invalidate(key: str) -> None:
    path = _cache_path(key)
    path.unlink(missing_ok=True)


def clear_expired(ttl_days: int = _DEFAULT_TTL_DAYS) -> int:
    """Remove all expired cache entries. Returns count removed."""
    removed = 0
    if not _CACHE_DIR.exists():
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)
    for path in _CACHE_DIR.glob("*.json"):
        try:
            envelope = json.loads(path.read_text())
            cached_at = datetime.fromisoformat(envelope["cached_at"])
            if cached_at < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        except Exception:
            pass
    return removed


def cache_key(*parts: str) -> str:
    return ":".join(parts)
