"""
store.py - tiny SQLite database remembering which listings the bot has seen
and alerted on, so you don't get pinged twice for the same item.

notify-side: the WhatsApp sender lives here too to keep the file count down.
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
from dataclasses import asdict, fields, is_dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Iterable, Optional

import requests

if TYPE_CHECKING:
    from pricing import Listing

DB_FILE = "seen_items.db"

_ITEMS_SCHEMA = """CREATE TABLE items (
                       source TEXT NOT NULL,
                       item_id TEXT NOT NULL,
                       title TEXT,
                       first_seen TEXT,
                       last_seen TEXT,
                       last_price INTEGER,
                       alerted_price INTEGER,
                       PRIMARY KEY (source, item_id)
                   )"""

_SNAPSHOTS_SCHEMA = """CREATE TABLE IF NOT EXISTS source_snapshots (
                           source TEXT PRIMARY KEY,
                           captured_at TEXT NOT NULL,
                           listing_count INTEGER NOT NULL,
                           payload_json TEXT NOT NULL
                       )"""


def _conn():
    c = sqlite3.connect(DB_FILE, timeout=30)
    c.execute("PRAGMA busy_timeout = 30000")
    _ensure_schema(c)
    return c


def _ensure_schema(c: sqlite3.Connection) -> None:
    """Create the current schema and migrate the original item-id-only table.

    SQLite DDL is transactional, so either the complete composite-key migration
    lands or the old table remains intact. Existing rows retain their source,
    timestamps, prices and alert state.
    """
    exists = c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='items'"
    ).fetchone()
    if not exists:
        with c:
            c.execute(_ITEMS_SCHEMA)
            c.execute(_SNAPSHOTS_SCHEMA)
        return

    info = c.execute("PRAGMA table_info(items)").fetchall()
    pk_columns = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
    if pk_columns != ["source", "item_id"]:
        with c:
            c.execute("DROP TABLE IF EXISTS items_composite_migration")
            c.execute(_ITEMS_SCHEMA.replace("items (", "items_composite_migration (", 1))
            c.execute(
                """INSERT INTO items_composite_migration
                       (source, item_id, title, first_seen, last_seen,
                        last_price, alerted_price)
                   SELECT
                       COALESCE(NULLIF(TRIM(source), ''), 'legacy'),
                       COALESCE(NULLIF(TRIM(item_id), ''), 'legacy-' || rowid),
                       title, first_seen, last_seen, last_price, alerted_price
                   FROM items"""
            )
            c.execute("DROP TABLE items")
            c.execute("ALTER TABLE items_composite_migration RENAME TO items")

    with c:
        c.execute(_SNAPSHOTS_SCHEMA)


def upsert_seen(item_id: str, source: str, title: str, price: int) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _conn() as c:
        c.execute(
            """INSERT INTO items (item_id, source, title, first_seen, last_seen, last_price)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(source, item_id) DO UPDATE SET
                   title=excluded.title,
                   last_seen=excluded.last_seen,
                   last_price=excluded.last_price""",
            (item_id, source, title, now, now, price),
        )


def prune_stale(days: int = 90) -> int:
    """Forget listings not seen for `days` days (long sold/removed) so the
    database doesn't grow forever. SQLite normalises the stored UTC ISO
    timestamps before comparison."""
    with _conn() as c:
        cur = c.execute(
            "DELETE FROM items WHERE datetime(last_seen) < datetime('now', ?)",
            (f"-{int(days)} days",),
        )
        return cur.rowcount


def alerted_price(source: str, item_id: str) -> Optional[int]:
    with _conn() as c:
        row = c.execute(
            "SELECT alerted_price FROM items WHERE source=? AND item_id=?",
            (source, item_id),
        ).fetchone()
    return row[0] if row else None


def mark_alerted(source: str, item_id: str, price: int) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE items SET alerted_price=? WHERE source=? AND item_id=?",
            (price, source, item_id),
        )


def should_alert(source: str, item_id: str, price: int,
                 realert_drop_pct: float) -> bool:
    prev = alerted_price(source, item_id)
    if prev is None:
        return True
    return price <= prev * (1 - realert_drop_pct / 100.0)


# ----------------------------------------------------------------------------
# Last-good per-source listing snapshots
# ----------------------------------------------------------------------------

def save_source_snapshot(source: str, listings: Iterable["Listing"]) -> bool:
    """Atomically save a source's non-empty, freshly fetched listing snapshot.

    Empty results commonly mean a marketplace block or layout change, so they
    deliberately do not erase the last known-good snapshot. Returns True only
    when a snapshot was written.
    """
    source = str(source).strip()
    if not source:
        raise ValueError("snapshot source must not be empty")

    records = []
    for listing in listings:
        # Never refresh a snapshot's age by saving a fallback-loaded listing.
        try:
            from_snapshot = float(
                getattr(listing, "snapshot_age_minutes", 0) or 0
            ) > 0
        except (TypeError, ValueError):
            from_snapshot = True
        if from_snapshot:
            continue
        if not is_dataclass(listing):
            raise TypeError("snapshot listings must be dataclass instances")
        record = asdict(listing)
        if str(record.get("source", "")).strip() != source:
            raise ValueError("all snapshot listings must belong to the given source")
        record.pop("snapshot_age_minutes", None)
        record.pop("snapshot_captured_at", None)
        records.append(record)

    if not records:
        return False

    payload = json.dumps(records, ensure_ascii=False, separators=(",", ":"),
                         allow_nan=False)
    captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _conn() as c:
        c.execute(
            """INSERT INTO source_snapshots
                   (source, captured_at, listing_count, payload_json)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(source) DO UPDATE SET
                   captured_at=excluded.captured_at,
                   listing_count=excluded.listing_count,
                   payload_json=excluded.payload_json""",
            (source, captured_at, len(records), payload),
        )
    return True


def load_source_snapshots(allowed_sources: Iterable[str],
                          fresh_sources: Iterable[str] = (),
                          max_age_minutes: float = 180.0) -> list["Listing"]:
    """Load recent last-good snapshots for allowed sources lacking fresh data.

    Unknown/removed Listing fields are ignored, required identity and price
    fields are validated, and malformed rows/listings are skipped. Rehydrated
    objects receive dynamic ``snapshot_age_minutes`` and
    ``snapshot_captured_at`` attributes for the report/UI.
    """
    allowed = list(dict.fromkeys(
        str(source).strip() for source in allowed_sources if str(source).strip()
    ))
    fresh = {str(source).strip() for source in fresh_sources}
    wanted = [source for source in allowed if source not in fresh]
    max_age = float(max_age_minutes)
    if not wanted or max_age < 0 or math.isnan(max_age):
        return []

    placeholders = ",".join("?" for _ in wanted)
    with _conn() as c:
        rows = c.execute(
            f"""SELECT source, captured_at, payload_json
                FROM source_snapshots
                WHERE source IN ({placeholders})""",
            wanted,
        ).fetchall()
    by_source = {row[0]: row for row in rows}

    from pricing import Listing

    init_fields = {field.name for field in fields(Listing) if field.init}
    loaded: list[Listing] = []
    now = datetime.now(timezone.utc)
    for source in wanted:
        row = by_source.get(source)
        if not row:
            continue
        captured = _parse_utc(row[1])
        if captured is None:
            continue
        age_minutes = max(0.0, (now - captured).total_seconds() / 60.0)
        if age_minutes > max_age:
            continue
        try:
            payload = json.loads(row[2])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, list):
            continue
        for record in payload:
            listing = _rehydrate_listing(Listing, init_fields, source, record)
            if listing is None:
                continue
            # A cached row is always marked >0, including one captured seconds
            # ago, so it cannot later be mistaken for a fresh source result.
            setattr(listing, "snapshot_age_minutes",
                    max(1, int(math.ceil(age_minutes))))
            setattr(listing, "snapshot_captured_at", captured.isoformat(timespec="seconds"))
            loaded.append(listing)
    return loaded


def get_snapshot_metadata(allowed_sources: Optional[Iterable[str]] = None) -> dict:
    """Return snapshot capture time, age and row count keyed by source."""
    allowed = None
    if allowed_sources is not None:
        allowed = list(dict.fromkeys(
            str(source).strip() for source in allowed_sources if str(source).strip()
        ))
        if not allowed:
            return {}

    sql = "SELECT source, captured_at, listing_count FROM source_snapshots"
    params: tuple = ()
    if allowed is not None:
        placeholders = ",".join("?" for _ in allowed)
        sql += f" WHERE source IN ({placeholders})"
        params = tuple(allowed)
    with _conn() as c:
        rows = c.execute(sql, params).fetchall()

    now = datetime.now(timezone.utc)
    result = {}
    for source, captured_text, count in rows:
        captured = _parse_utc(captured_text)
        age = (max(0.0, (now - captured).total_seconds() / 60.0)
               if captured is not None else None)
        result[source] = {
            "captured_at": captured_text,
            "age_minutes": round(age, 1) if age is not None else None,
            "listing_count": int(count),
        }
    return result


def _parse_utc(value: object) -> Optional[datetime]:
    try:
        text = str(value).strip()
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _rehydrate_listing(listing_cls, init_fields: set[str], source: str,
                       record: object):
    if not isinstance(record, dict):
        return None
    data = {key: value for key, value in record.items() if key in init_fields}
    try:
        item_id = str(data.get("item_id", "")).strip()
        title = str(data.get("title", "")).strip()
        price = float(data.get("price"))
    except (TypeError, ValueError, OverflowError):
        return None
    if not item_id or not title or not math.isfinite(price) or price <= 0:
        return None
    data.update(item_id=item_id, source=source, title=title, price=price)
    if "flags" in data and not isinstance(data["flags"], list):
        data["flags"] = []
    data.pop("snapshot_age_minutes", None)
    data.pop("snapshot_captured_at", None)
    try:
        return listing_cls(**data)
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------------
# WhatsApp (via CallMeBot's free personal-use API - see README step C)
# ----------------------------------------------------------------------------

_last_send = [0.0]     # CallMeBot is rate-limited; space messages politely


def whatsapp_send(cfg: dict, text: str) -> bool:
    w = cfg.get("whatsapp", {})
    if not w.get("enabled"):
        return False
    phone, apikey = str(w.get("phone", "")), str(w.get("apikey", ""))
    if "PASTE" in phone or "PASTE" in apikey or not phone or not apikey:
        print("  [whatsapp] not configured - fill whatsapp section of config.yaml")
        return False
    gap = time.time() - _last_send[0]
    if gap < 3:
        time.sleep(3 - gap)
    try:
        r = requests.get(
            "https://api.callmebot.com/whatsapp.php",
            params={"phone": phone, "text": text, "apikey": apikey},
            timeout=30,
        )
        _last_send[0] = time.time()
        ok = r.status_code == 200 and "ERROR" not in r.text.upper()[:400]
        if not ok:
            print(f"  [whatsapp] send failed (HTTP {r.status_code}): {r.text[:200]}")
        return bool(ok)
    except Exception as e:
        print(f"  [whatsapp] send failed: {e}")
        return False
