"""Local SQLite storage for the wishlist.

Each function opens its own short-lived connection, since SQLite connections are cheap and
this also lets MCP resources (which do not receive the request context) read the wishlist.

Games are looked up either by id, which is exact and uses the primary key, or by title, which
is case-insensitive; the two are kept apart so that a title can never match an id by accident.
ITAD ids are lowercase UUIDs, so ids are lowercased on the way in, which keeps one row per game
however the id was typed.
"""

import re
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from deals_mcp.matching import fold

SCHEMA = """
CREATE TABLE IF NOT EXISTS wishlist (
    game_id TEXT PRIMARY KEY,
    game_title TEXT NOT NULL,
    country TEXT NOT NULL DEFAULT 'US',
    min_discount_percent INTEGER NOT NULL DEFAULT 0,
    max_price REAL DEFAULT NULL,
    added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

COLUMNS = "game_id, game_title, country, min_discount_percent, max_price, added_at"


ITAD_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# Title matching ignores case, accents and extra whitespace, using matching.fold, because SQLite's
# LOWER() and COLLATE NOCASE only handle ASCII ("POKÉMON" would not match "Pokémon").
BY_ID = "game_id = ?"
BY_TITLE = "FOLD(game_title) = FOLD(?) ORDER BY game_id"


def _fold(value):
    return fold(value) if isinstance(value, str) else value


def normalize_id(game_id: str) -> str:
    return game_id.strip().lower()


def is_itad_id(game_id: str) -> bool:
    return isinstance(game_id, str) and ITAD_ID.fullmatch(game_id) is not None


@asynccontextmanager
async def _connect(db_path: str):
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.create_function("FOLD", 1, _fold, deterministic=True)
        yield conn


async def _select(conn, where: str, params: tuple) -> list[dict]:
    async with conn.execute(f"SELECT {COLUMNS} FROM wishlist WHERE {where}", params) as cursor:
        return [dict(row) for row in await cursor.fetchall()]


async def init_db(db_path: str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    async with _connect(db_path) as conn:
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute(SCHEMA)
        await conn.commit()


async def add_item(db_path, game_id, game_title, country, min_discount_percent=0, max_price=None) -> dict:
    """Add a game, or replace the title, country and thresholds of one already on the wishlist.

    An upsert is used rather than INSERT OR REPLACE, since a replace deletes the row and so would
    reset added_at. A max_price of 0 or less means no cap, the same as in set_thresholds.
    """
    game_id = normalize_id(game_id)
    # Checked here as well as in the tools, so that no caller can store an id that would later make
    # ITAD reject a whole batched price request.
    if not is_itad_id(game_id):
        raise ValueError(f"'{game_id}' is not an IsThereAnyDeal game id.")
    game_title = game_title.strip()
    max_price = max_price if max_price and max_price > 0 else None
    async with _connect(db_path) as conn:
        async with conn.execute(
            "INSERT INTO wishlist (game_id, game_title, country, min_discount_percent, max_price) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(game_id) DO UPDATE SET game_title = excluded.game_title, country = excluded.country, "
            f"min_discount_percent = excluded.min_discount_percent, max_price = excluded.max_price RETURNING {COLUMNS}",
            (game_id, game_title, country, min_discount_percent, max_price),
        ) as cursor:
            row = await cursor.fetchone()
        await conn.commit()
    return dict(row)


async def get_item(db_path, game_id) -> dict | None:
    """Look up one game by its id only."""
    async with _connect(db_path) as conn:
        rows = await _select(conn, BY_ID, (normalize_id(game_id),))
    return rows[0] if rows else None


async def find_by_id_or_title(db_path, game_id_or_title) -> list[dict]:
    """The game with this id, or else every game with this title, using a single connection.

    The id is tried first, so that a title which happens to look like another game's id is never
    picked up instead of that game.
    """
    async with _connect(db_path) as conn:
        return await _select(conn, BY_ID, (normalize_id(game_id_or_title),)) or await _select(
            conn, BY_TITLE, (game_id_or_title,)
        )


async def list_items(db_path) -> list[dict]:
    async with _connect(db_path) as conn:
        async with conn.execute(f"SELECT {COLUMNS} FROM wishlist") as cursor:
            items = [dict(row) for row in await cursor.fetchall()]
    # Sorted here, ignoring case and accents in the same way as title matching, so that "Ōkami"
    # sorts next to "okami 2".
    return sorted(items, key=lambda i: (fold(i["game_title"]), i["game_id"]))


async def remove_item(db_path, game_id) -> bool:
    async with _connect(db_path) as conn:
        cursor = await conn.execute("DELETE FROM wishlist WHERE game_id = ?", (normalize_id(game_id),))
        await conn.commit()
        return cursor.rowcount > 0


async def set_thresholds(db_path, game_id, min_discount_percent=None, max_price=None) -> dict | None:
    """Update only the thresholds that are given; returns the updated item, or None if it is not found.

    A max_price of 0 or less removes the price cap.
    """
    updates, values = [], []
    if min_discount_percent is not None:
        updates.append("min_discount_percent = ?")
        values.append(min_discount_percent)
    if max_price is not None:
        updates.append("max_price = ?")
        values.append(max_price if max_price > 0 else None)

    if not updates:
        return await get_item(db_path, game_id)
    async with _connect(db_path) as conn:
        async with conn.execute(
            f"UPDATE wishlist SET {', '.join(updates)} WHERE game_id = ? RETURNING {COLUMNS}",
            (*values, normalize_id(game_id)),
        ) as cursor:
            row = await cursor.fetchone()
        await conn.commit()
    return dict(row) if row else None
