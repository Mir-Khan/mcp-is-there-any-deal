import unicodedata

import aiosqlite
import pytest

from deals_mcp import db

pytestmark = pytest.mark.anyio


def uid(n: int) -> str:
    """A readable fake ITAD id, since the database only stores ids in ITAD's UUID format."""
    return f"00000000-0000-4000-8000-{n:012d}"


ID1, ID2, ID3, ID4 = uid(1), uid(2), uid(3), uid(4)
MISSING = uid(999)


@pytest.fixture
async def db_path(tmp_path):
    path = str(tmp_path / "wishlist.db")
    await db.init_db(path)
    return path


async def ids_for_title(db_path, title):
    return [i["game_id"] for i in await db.find_by_id_or_title(db_path, title)]


async def test_add_and_list(db_path):
    await db.add_item(db_path, ID1, "Hollow Knight", "US")
    await db.add_item(db_path, ID2, "Celeste", "GB", min_discount_percent=50, max_price=5.0)
    items = await db.list_items(db_path)
    assert [i["game_title"] for i in items] == ["Celeste", "Hollow Knight"]
    assert items[0]["max_price"] == 5.0


async def test_add_replaces_existing(db_path):
    await db.add_item(db_path, ID1, "Hollow Knight", "US")
    await db.add_item(db_path, ID1, "Hollow Knight", "CA", min_discount_percent=30)
    items = await db.list_items(db_path)
    assert len(items) == 1
    assert items[0]["country"] == "CA"


async def test_only_itad_ids_can_be_stored(db_path):
    for bad in ("Hades", "", "id-1", ID1 + "0"):
        with pytest.raises(ValueError, match="not an IsThereAnyDeal game id"):
            await db.add_item(db_path, bad, "Hades", "US")
    assert await db.list_items(db_path) == []


async def test_remove_by_id(db_path):
    await db.add_item(db_path, ID1, "Hollow Knight", "US")
    await db.add_item(db_path, ID2, "Celeste", "US")
    assert await db.remove_item(db_path, ID2.upper())
    assert not await db.remove_item(db_path, MISSING)
    assert [i["game_id"] for i in await db.list_items(db_path)] == [ID1]


async def test_set_thresholds(db_path):
    await db.add_item(db_path, ID1, "Hollow Knight", "US", max_price=10.0)
    item = await db.set_thresholds(db_path, ID1, min_discount_percent=40)
    assert item["min_discount_percent"] == 40
    assert item["max_price"] == 10.0

    item = await db.set_thresholds(db_path, ID1, max_price=0)
    assert item["max_price"] is None

    assert await db.set_thresholds(db_path, MISSING, min_discount_percent=10) is None


# ---- Edge cases ----


async def test_titles_with_quotes_and_unicode(db_path):
    await db.add_item(db_path, ID1, "Assassin's Creed", "US")
    await db.add_item(db_path, ID2, 'Pokémon "Special" Edition', "US")
    assert await ids_for_title(db_path, "assassin's creed") == [ID1]
    # SQLite's LOWER() is ASCII-only, so this relies on the FOLD function registered in _connect.
    assert await ids_for_title(db_path, 'POKÉMON "SPECIAL" EDITION') == [ID2]


async def test_title_matching_ignores_accents_unicode_forms_and_spacing(db_path):
    await db.add_item(db_path, ID1, "Pokémon", "US")
    await db.add_item(db_path, ID2, "Ōkami HD", "US")
    # The same accented letter written as one character (NFC) or as a letter plus accent (NFD).
    assert await ids_for_title(db_path, unicodedata.normalize("NFD", "Pokémon")) == [ID1]
    assert await ids_for_title(db_path, "pokemon") == [ID1]
    assert await ids_for_title(db_path, "  okami   hd ") == [ID2]


async def test_titles_are_stored_without_surrounding_whitespace(db_path):
    item = await db.add_item(db_path, ID1, "  Hades ", "US")
    assert item["game_title"] == "Hades"
    assert await ids_for_title(db_path, "Hades") == [ID1]


async def test_sql_like_input_is_treated_as_plain_text(db_path):
    await db.add_item(db_path, ID1, "Hollow Knight", "US")
    assert not await db.remove_item(db_path, "' OR 1=1 --")
    assert not await db.remove_item(db_path, "%")
    assert await db.find_by_id_or_title(db_path, "%") == []
    assert len(await db.list_items(db_path)) == 1


async def test_a_shared_title_returns_every_match_in_a_stable_order(db_path):
    await db.add_item(db_path, ID2, "Doom", "US")
    await db.add_item(db_path, ID1, "DOOM", "US")
    assert await ids_for_title(db_path, " doom ") == [ID1, ID2]


async def test_id_operations_never_match_titles(db_path):
    # A game whose title is the same text as another game's id must not be touched by id operations.
    await db.add_item(db_path, ID1, "Real Game", "US")
    await db.add_item(db_path, ID2, ID1, "US")
    assert (await db.get_item(db_path, ID1))["game_title"] == "Real Game"
    assert (await db.set_thresholds(db_path, ID1, max_price=5))["game_title"] == "Real Game"
    assert (await db.get_item(db_path, ID2))["max_price"] is None
    assert await db.remove_item(db_path, ID1)
    assert [i["game_id"] for i in await db.list_items(db_path)] == [ID2]
    assert await db.get_item(db_path, "Real Game") is None


async def test_find_by_id_or_title_prefers_the_id(db_path):
    await db.add_item(db_path, ID1, "Real Game", "US")
    await db.add_item(db_path, ID2, ID1.upper(), "US")
    assert await ids_for_title(db_path, f" {ID1.upper()} ") == [ID1]
    assert await ids_for_title(db_path, "real game") == [ID1]
    assert await db.find_by_id_or_title(db_path, "missing") == []


async def test_ids_are_case_insensitive_and_stored_once(db_path):
    await db.add_item(db_path, ID1.upper(), "Hollow Knight", "US")
    await db.add_item(db_path, ID1, "Hollow Knight", "GB")
    items = await db.list_items(db_path)
    assert [(i["game_id"], i["country"]) for i in items] == [(ID1, "GB")]
    assert (await db.get_item(db_path, f" {ID1.upper()} "))["country"] == "GB"


async def test_re_adding_a_game_keeps_its_added_at(db_path):
    await db.add_item(db_path, ID1, "Hollow Knight", "US", max_price=10)
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("UPDATE wishlist SET added_at = '2020-01-01 00:00:00'")
        await conn.commit()
    item = await db.add_item(db_path, ID1, "Hollow Knight: Voidheart Edition", "CA")
    assert item["added_at"] == "2020-01-01 00:00:00"
    # Re-adding replaces the title, country and thresholds, including clearing the old price cap.
    assert (item["game_title"], item["country"], item["max_price"]) == ("Hollow Knight: Voidheart Edition", "CA", None)


async def test_a_max_price_of_zero_means_no_cap(db_path):
    assert (await db.add_item(db_path, ID1, "Hollow Knight", "US", max_price=0))["max_price"] is None


async def test_set_thresholds_with_nothing_to_change(db_path):
    await db.add_item(db_path, ID1, "Hollow Knight", "US", min_discount_percent=20, max_price=8.0)
    item = await db.set_thresholds(db_path, ID1)
    assert (item["min_discount_percent"], item["max_price"]) == (20, 8.0)


async def test_init_db_creates_missing_folders_and_is_repeatable(tmp_path):
    path = str(tmp_path / "nested" / "folder" / "wishlist.db")
    await db.init_db(path)
    await db.add_item(path, ID1, "Hollow Knight", "US")
    await db.init_db(path)
    assert len(await db.list_items(path)) == 1


async def test_list_ignores_case_and_accents_when_sorting(db_path):
    for game_id, title in ((ID1, "zelda"), (ID2, "Ōkami"), (ID3, "okami 2"), (ID4, "Abzû")):
        await db.add_item(db_path, game_id, title, "US")
    assert [i["game_title"] for i in await db.list_items(db_path)] == ["Abzû", "Ōkami", "okami 2", "zelda"]
