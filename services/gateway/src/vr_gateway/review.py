"""Leitner spaced review of saved expressions (boxes 1-5, intervals 0/1/3/7/14 days)."""

from __future__ import annotations

import time

from .db import Database
from .errors import ApiError

INTERVAL_DAYS = [0, 1, 3, 7, 14]
DAY = 86400


def due_items(db: Database, now: float | None = None, limit: int = 50) -> list[dict]:
    now = now or time.time()
    return db.query(
        "SELECT item_id, text_en, text_ko, source_type, source_id, box, due_at, last_result FROM review_items"
        " WHERE due_at <= ? ORDER BY due_at LIMIT ?", (now, limit))


def grade(db: Database, item_id: str, result: str, now: float | None = None) -> dict:
    if result not in ("again", "good"):
        raise ApiError("INVALID_REQUEST")
    now = now or time.time()
    rows = db.query("SELECT box FROM review_items WHERE item_id = ?", (item_id,))
    if not rows:
        raise ApiError("NOT_FOUND")
    box = 1 if result == "again" else min(5, rows[0]["box"] + 1)
    due = now + INTERVAL_DAYS[box - 1] * DAY
    with db.tx() as c:
        c.execute("UPDATE review_items SET box = ?, due_at = ?, last_result = ? WHERE item_id = ?",
                  (box, due, result, item_id))
    return {"item_id": item_id, "box": box, "due_at": due}
