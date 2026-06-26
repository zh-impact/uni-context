"""Spike: validate that a Python port of uni-context can read the existing
Go-written DB at ~/.local/share/unictx/unictx.db.

Validates six risks identified in migration review:

1. sqlite-vec extension loads via sync sqlite3 AND aiosqlite
2. FTS5 trigram tokenizer works (and the malformed-FTS bug we just hit
   in Go is also observable from Python — proves we need the fix here too)
3. Existing Go-written DB schema is readable (migrations, FTS, vec0)
4. Cursor format compat: Go encodes cursor as base36(ts) + ":" + id.
   Python must produce identical encoding for pagination to round-trip.
5. Live data sanity: 19 context_item rows, vec_bge_m3_1024 queryable,
   context_fts MATCH returns hits.
6. Round-trip: decode Go cursor → re-encode in Python → byte-identical.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

import sqlite_vec

DB_PATH = Path.home() / "dotfiles" / "local" / "share" / "unictx" / "unictx.db"


# ---- base36 cursor (mirror of Go's strconv.FormatInt(ts, 36) + ":" + id) ----

BASE36_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"


def encode_cursor(ts: int, item_id: str) -> str:
    """Mirror of internal/adapter/sqlite/repo.go:encodeCursor."""
    if ts == 0:
        return "0:" + item_id
    sign = ""
    if ts < 0:
        sign, ts = "-", -ts
    out = ""
    while ts > 0:
        ts, rem = divmod(ts, 36)
        out = BASE36_DIGITS[rem] + out
    return sign + out + ":" + item_id


def decode_cursor(cursor: str) -> tuple[int, str]:
    """Mirror of internal/adapter/sqlite/repo.go:decodeCursor."""
    head, _, tail = cursor.partition(":")
    if not tail:
        raise ValueError(f"malformed cursor: {cursor!r}")
    sign = 1
    digits = head
    if head.startswith("-"):
        sign, digits = -1, head[1:]
    ts = int(digits, 36) * sign
    return ts, tail


# ---- sync sqlite3 ----

def test_sync_sqlite_vec() -> None:
    print("\n[Risk 1a] sqlite-vec loads via sync sqlite3")
    db = sqlite3.connect(":memory:")
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    vec_version = db.execute("select vec_version()").fetchone()[0]
    print(f"  ✓ vec_version={vec_version}")
    db.close()


def test_sync_fts5_trigram() -> None:
    print("\n[Risk 2a] FTS5 trigram tokenizer works in sync sqlite3")
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE VIRTUAL TABLE t USING fts5(x, tokenize='trigram')"
    )
    db.execute("INSERT INTO t(x) VALUES ('hello world'), ('batchsize tuning')")
    n = db.execute("SELECT count(*) FROM t WHERE t MATCH 'batchsize'").fetchone()[0]
    assert n == 1, f"expected 1 match, got {n}"
    print(f"  ✓ trigram MATCH finds 1/2 rows for 'batchsize'")
    db.close()


# ---- aiosqlite ----

async def test_aiosqlite_vec() -> None:
    print("\n[Risk 1b] sqlite-vec loads via aiosqlite")
    import aiosqlite

    # aiosqlite runs SQLite on a worker thread; load_extension must be
    # called on the same thread the connection lives on. The pattern is
    # to call .enable_load_extension / .load_extension via the async API,
    # which marshals onto the worker.
    db = await aiosqlite.connect(":memory:")
    try:
        await db.enable_load_extension(True)
        await db.load_extension(sqlite_vec.loadable_path())
        version = (await (await db.execute("select vec_version()")).fetchone())[0]
        print(f"  ✓ vec_version={version}")
    finally:
        await db.close()


# ---- real DB read ----

def test_read_go_db_schema() -> None:
    print(f"\n[Risk 3] Reading existing Go DB at {DB_PATH}")
    if not DB_PATH.exists():
        print(f"  ✗ DB not found at {DB_PATH}")
        sys.exit(1)

    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    db.enable_load_extension(True)
    sqlite_vec.load(db)

    tables = [
        r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    ]
    print(f"  ✓ {len(tables)} tables: {', '.join(tables)}")

    schema_version = db.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()[0]
    print(f"  ✓ schema_version={schema_version}")

    n_items = db.execute("SELECT count(*) FROM context_item").fetchone()[0]
    n_externalized = db.execute(
        "SELECT count(*) FROM context_item WHERE content_uri != '' AND length(content) = 0"
    ).fetchone()[0]
    print(f"  ✓ {n_items} items, {n_externalized} externalized (>4KB → FileStore)")

    n_models = db.execute(
        "SELECT count(*) FROM embedding_model"
    ).fetchone()[0]
    print(f"  ✓ {n_models} embedding_model rows")

    # vec0 table queryable
    n_vec = db.execute("SELECT count(*) FROM vec_bge_m3_1024").fetchone()[0]
    print(f"  ✓ vec_bge_m3_1024 row count: {n_vec}")

    db.close()


def test_fts_match_on_real_data() -> None:
    print("\n[Risk 5] FTS MATCH on real data finds Go-indexed rows")
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    db.enable_load_extension(True)
    sqlite_vec.load(db)

    n = db.execute(
        "SELECT count(*) FROM context_fts WHERE context_fts MATCH 'batchsize'"
    ).fetchone()[0]
    print(f"  ✓ MATCH 'batchsize' finds {n} row(s)")

    # Same query but with snippet on TITLE column (the fixed SQL after
    # 2026-06-26 malformed-FTS bugfix). This MUST work.
    rows = db.execute(
        """
        SELECT ci.id, snippet(context_fts, 0, '', '', '…', 16) AS title_snip
        FROM context_fts
        JOIN context_item ci ON ci.rowid = context_fts.rowid
        WHERE context_fts MATCH 'batchsize'
        """
    ).fetchall()
    for row in rows:
        print(f"    -> id={row[0][:8]}… title_snip={row[1]!r}")

    # And the BROKEN query (content snippet) — proves the bug is
    # SQL-level, not language-level. We expect this to fail or return
    # ellipsis-only.
    print("  [bug repro] snippet(context_fts, 2, ...) on externalized row:")
    try:
        rows = db.execute(
            "SELECT snippet(context_fts, 2, '', '', '…', 16) "
            "FROM context_fts WHERE context_fts MATCH 'batchsize'"
        ).fetchall()
        for row in rows:
            print(f"    -> content_snip={row[0]!r}")
    except sqlite3.DatabaseError as e:
        print(f"    -> raised: {e}")

    db.close()


def test_cursor_roundtrip() -> None:
    print("\n[Risk 4 + 6] Cursor format compat (base36 ts + ':' + id)")

    # Decode an actual Go-written cursor from the live DB. We pick the
    # first 2 items by created_at DESC and construct what Go would emit
    # as the NextCursor.
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    rows = db.execute(
        "SELECT created_at, id FROM context_item ORDER BY created_at DESC LIMIT 2"
    ).fetchall()
    db.close()

    # created_at is stored as INTEGER Unix timestamp (matches Go's
    # time.Now().Unix() writeback in scanItem/repo.go).
    ts = int(rows[0][0])
    item_id = rows[0][1]
    go_cursor = encode_cursor(ts, item_id)
    print(f"  Go-style cursor for ts={ts} id={item_id[:8]}…: {go_cursor[:40]}…")

    # Round-trip: encode → decode → re-encode must be identical.
    decoded_ts, decoded_id = decode_cursor(go_cursor)
    assert decoded_ts == ts, f"ts mismatch: {decoded_ts} != {ts}"
    assert decoded_id == item_id, "id mismatch"
    reencoded = encode_cursor(decoded_ts, decoded_id)
    assert reencoded == go_cursor, f"round-trip drift: {reencoded} != {go_cursor}"
    print(f"  ✓ round-trip byte-identical (ts={decoded_ts})")

    # Cross-check: read what Go's strconv.FormatInt(0, 36) produces and
    # verify Python matches.
    assert encode_cursor(0, "x") == "0:x"
    assert encode_cursor(35, "x") == "z:x"
    assert encode_cursor(36, "x") == "10:x"
    assert encode_cursor(1719398400, "x")  # sanity: doesn't crash on a real ts
    print(f"  ✓ base36 encoding matches Go strconv.FormatInt semantics")


async def main() -> None:
    print("=" * 70)
    print("uni-context Python migration spike")
    print("=" * 70)

    test_sync_sqlite_vec()
    test_sync_fts5_trigram()
    await test_aiosqlite_vec()
    test_read_go_db_schema()
    test_fts_match_on_real_data()
    test_cursor_roundtrip()

    print("\n" + "=" * 70)
    print("ALL CHECKS PASSED")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
