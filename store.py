"""
store.py - tiny SQLite database remembering which listings the bot has seen
and alerted on, so you don't get pinged twice for the same item.

notify-side: Telegram sender lives here too to keep the file count down.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional

import requests

DB_FILE = "seen_items.db"


def _conn():
    c = sqlite3.connect(DB_FILE)
    c.execute(
        """CREATE TABLE IF NOT EXISTS items (
               item_id TEXT PRIMARY KEY,
               source TEXT,
               title TEXT,
               first_seen TEXT,
               last_seen TEXT,
               last_price INTEGER,
               alerted_price INTEGER
           )"""
    )
    return c


def upsert_seen(item_id: str, source: str, title: str, price: int) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _conn() as c:
        c.execute(
            """INSERT INTO items (item_id, source, title, first_seen, last_seen, last_price)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(item_id) DO UPDATE SET last_seen=?, last_price=?""",
            (item_id, source, title, now, now, price, now, price),
        )


def alerted_price(item_id: str) -> Optional[int]:
    with _conn() as c:
        row = c.execute("SELECT alerted_price FROM items WHERE item_id=?", (item_id,)).fetchone()
    return row[0] if row else None


def mark_alerted(item_id: str, price: int) -> None:
    with _conn() as c:
        c.execute("UPDATE items SET alerted_price=? WHERE item_id=?", (price, item_id))


def should_alert(item_id: str, price: int, realert_drop_pct: float) -> bool:
    prev = alerted_price(item_id)
    if prev is None:
        return True
    return price <= prev * (1 - realert_drop_pct / 100.0)


# ----------------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------------

def telegram_send(cfg: dict, text: str) -> bool:
    t = cfg.get("telegram", {})
    if not t.get("enabled"):
        return False
    token, chat_id = t.get("bot_token", ""), str(t.get("chat_id", ""))
    if "PASTE" in token or not token or not chat_id or "PASTE" in chat_id:
        print("  [telegram] not configured - fill telegram section of config.yaml")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": False},
            timeout=15,
        )
        ok = r.status_code == 200 and r.json().get("ok")
        if not ok:
            print(f"  [telegram] send failed: {r.text[:200]}")
        return bool(ok)
    except Exception as e:
        print(f"  [telegram] send failed: {e}")
        return False
