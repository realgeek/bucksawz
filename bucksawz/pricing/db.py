"""SQLite-backed price cache for AWS Pricing API data."""
from __future__ import annotations
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_DEFAULT_DB = Path(
    os.environ.get(
        "BUCKSAWZ_PRICE_DB",
        str(
            Path(os.environ.get("BUCKSAWZ_CACHE_DIR", Path.home() / ".cache" / "bucksawz"))
            / "prices.db"
        ),
    )
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    service     TEXT NOT NULL,
    region      TEXT NOT NULL,
    price_key   TEXT NOT NULL,
    unit        TEXT NOT NULL,
    price_usd   REAL NOT NULL,
    description TEXT,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (service, region, price_key)
);
"""


def _connect(path: Optional[Path] = None) -> sqlite3.Connection:
    p = path or _DEFAULT_DB
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute(_SCHEMA)
    conn.commit()
    return conn


def upsert(
    service: str,
    region: str,
    price_key: str,
    unit: str,
    price_usd: float,
    description: str = "",
    db: Optional[Path] = None,
) -> None:
    with _connect(db) as conn:
        conn.execute(
            """
            INSERT INTO prices (service, region, price_key, unit, price_usd, description, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(service, region, price_key) DO UPDATE SET
                unit        = excluded.unit,
                price_usd   = excluded.price_usd,
                description = excluded.description,
                fetched_at  = excluded.fetched_at
            """,
            (
                service, region, price_key, unit, price_usd, description,
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def get_price(
    service: str, region: str, price_key: str, db: Optional[Path] = None
) -> Optional[dict]:
    with _connect(db) as conn:
        row = conn.execute(
            "SELECT * FROM prices WHERE service=? AND region=? AND price_key=?",
            (service, region, price_key),
        ).fetchone()
        return dict(row) if row else None


def get_all(service: str, region: str, db: Optional[Path] = None) -> list[dict]:
    with _connect(db) as conn:
        rows = conn.execute(
            "SELECT * FROM prices WHERE service=? AND region=? ORDER BY price_key",
            (service, region),
        ).fetchall()
        return [dict(r) for r in rows]


def count(db: Optional[Path] = None) -> int:
    with _connect(db) as conn:
        return conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]


def service_summary(db: Optional[Path] = None) -> list[dict]:
    """Return per-service/region row counts and freshest fetch timestamp."""
    with _connect(db) as conn:
        rows = conn.execute(
            """
            SELECT service, region, COUNT(*) as rows, MAX(fetched_at) as last_fetched
            FROM prices GROUP BY service, region ORDER BY service, region
            """
        ).fetchall()
        return [dict(r) for r in rows]


def db_path() -> Path:
    return _DEFAULT_DB
