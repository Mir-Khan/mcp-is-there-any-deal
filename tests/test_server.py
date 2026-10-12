"""End-to-end tests through an in-process MCP client, with the ITAD API replaced by a fake.

The fake serves real search results saved in fixtures/itad_search.json, along with canned prices.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

import aiosqlite
import anyio
import pytest
from mcp import Client
from mcp_types import ElicitResult

from deals_mcp import config, server
from deals_mcp.itad import ITADClient, ITADError

pytestmark = pytest.mark.anyio

# ITAD ids are UUIDs, and the tools reject anything else, so the fake games use readable UUIDs.
HK = "00000000-0000-4000-8000-000000000001"
CELESTE = "00000000-0000-4000-8000-000000000002"
FULL_PRICE = "00000000-0000-4000-8000-000000000003"
FAKE_SALE = "00000000-0000-4000-8000-000000000004"
DUPE_A = "00000000-0000-4000-8000-000000000005"
DUPE_B = "00000000-0000-4000-8000-000000000006"
DUPE_C = "00000000-0000-4000-8000-000000000007"
DOOM_1993 = "00000000-0000-4000-8000-000000000008"
DOOM_2016 = "00000000-0000-4000-8000-000000000009"
UNKNOWN = "00000000-0000-4000-8000-000000000010"
DELISTED = "00000000-0000-4000-8000-000000000011"
OTHER = "00000000-0000-4000-8000-000000000012"
MARKET = "00000000-0000-4000-8000-000000000013"
NO_SHOPS = "00000000-0000-4000-8000-000000000014"
ODD_DATA = "00000000-0000-4000-8000-000000000015"

SEARCHES = json.loads((Path(__file__).parent / "fixtures" / "itad_search.json").read_text(encoding="utf-8"))


def make_deal(shop, price, regular, cut):
    return {
        "shop": {"name": shop},
        "price": {"amount": price, "currency": "USD"},
        "regular": {"amount": regular, "currency": "USD"},
        "cut": cut,
        "url": f"https://example.com/{shop}",
        "drm": [],
    }


FAKE_PRICES = {
    HK: [make_deal("Steam", 7.49, 14.99, 50), make_deal("GOG", 14.99, 14.99, 0)],
    CELESTE: [make_deal("Steam", 4.99, 19.99, 75)],
    FULL_PRICE: [make_deal("Steam", 29.99, 29.99, 0)],
    # A small discount at one shop that is still dearer than another shop's normal price.
    # 50% off at a shop whose regular price is $60, but another shop's regular price is $35, so
    # it is only 14% off the market price.
    MARKET: [make_deal("Pricey Shop", 30.0, 60.0, 50), make_deal("Fair Shop", 35.0, 35.0, 0)],
    # ITAD can send null for a game with no shops.
    NO_SHOPS: None,
    # A drm list of plain strings rather than objects, which the summary cannot read.
    ODD_DATA: [{**make_deal("Odd Shop", 1.0, 10.0, 90), "drm": ["Steam"]}],
    FAKE_SALE: [make_deal("GamesPlanet", 72.58, 80.65, 10), make_deal("Microsoft Store", 69.99, 69.99, 0)],
}

# Two different games that ITAD lists with the same title and type, told apart only by slug.
DUPLICATE_TITLES = [
    {"id": DUPE_A, "slug": "shadow-quest", "title": "Shadow Quest", "type": "game"},
    {"id": DUPE_B, "slug": "shadow-quest-2009", "title": "Shadow Quest", "type": "game"},
    {"id": DUPE_C, "slug": "shadow-quest-remastered", "title": "Shadow Quest Remastered", "type": "game"},
]

FAKE_OVERVIEW = {
    HK: {
        "lowest": {"shop": {"name": "GOG"}, "price": {"amount": 5.09, "currency": "USD"}, "cut": 66, "timestamp": "2020-11-27"},
        "current": {"shop": {"name": "Steam"}, "price": {"amount": 7.49, "currency": "USD"}, "cut": 50, "timestamp": "2026-10-01"},
        "bundled": 5,
    }
}


@dataclass
class FakeITAD:
    """Stands in for the ITAD API and records every call, so that tests can check batching."""

    calls: list = field(default_factory=list)
    failing_countries: set = field(default_factory=set)
    search_overrides: dict = field(default_factory=dict)
    price_delay: float = 0
    in_flight: int = 0
    max_in_flight: int = 0

    async def search(self, title, results=50):
        self.calls.append(("search", title, results))
        if title in self.search_overrides:
            return self.search_overrides[title]
        if title == "Shadow Quest":
            return DUPLICATE_TITLES
        return SEARCHES.get(title, [])

    async def prices(self, game_ids, country):
        self.calls.append(("prices", country, sorted(game_ids)))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await anyio.sleep(self.price_delay)
        finally:
            self.in_flight -= 1
        if country in self.failing_countries:
            raise ITADError(f"Prices are unavailable for {country}.")
        # A game that ITAD does not know is simply missing from the response.
        return [{"id": gid, "deals": FAKE_PRICES[gid]} for gid in game_ids if gid in FAKE_PRICES]

    async def overview(self, game_ids, country):
        self.calls.append(("overview", country, game_ids))
        return {"prices": [{"id": gid, **FAKE_OVERVIEW[gid]} for gid in game_ids if gid in FAKE_OVERVIEW]}


@pytest.fixture
def fake_itad(tmp_path, monkeypatch):
    monkeypatch.setenv("DEALS_DB_PATH", str(tmp_path / "wishlist.db"))
    monkeypatch.delenv("DEFAULT_COUNTRY", raising=False)
    fake = FakeITAD()
    for name in ("search", "prices", "overview"):
        monkeypatch.setattr(ITADClient, name, lambda self, *a, _n=name, **kw: getattr(fake, _n)(*a, **kw))
    return fake


@pytest.fixture
async def client(fake_itad):
    """A client that cannot show elicitation forms, like a client without that feature."""
    async with Client(server.mcp) as c:
        yield c


class Chooser:
    """An elicitation callback that plays the user, picking an option by its title (or declining)."""

    def __init__(self, pick=None):
        self.pick = pick
        self.asked = []

    async def __call__(self, context, params):
        options = params.requested_schema["properties"]["game"]["enum"]
        self.asked.append((params.message, options))
        if self.pick is None:
            return ElicitResult(action="decline")
        label = next((o for o in options if o.startswith(self.pick + " (")), None)
        label = label or next(o for o in options if o.startswith(self.pick))
        return ElicitResult(action="accept", content={"game": label})


async def call(client, name, args=None):
    result = await client.call_tool(name, args or {})
    assert not result.is_error, result.content
    return result.structured_content


async def call_error(client, name, args=None) -> str:
    result = await client.call_tool(name, args or {})
    assert result.is_error, result.structured_content
    return result.content[0].text


async def insert_legacy_row(game_id, title):
    """Store a row directly, as an older version of the server could, bypassing the id checks."""
    async with aiosqlite.connect(server.get_db_path()) as conn:
        await conn.execute("INSERT INTO wishlist (game_id, game_title, country) VALUES (?, ?, 'US')", (game_id, title))
        await conn.commit()


def titles(found):
    return [r["title"] for r in found["results"]]


# ---- Basics ----


async def test_lists_all_primitives(client):
    tools = {t.name for t in (await client.list_tools()).tools}
    assert tools == {
        "search_game", "get_current_deals", "get_historical_low", "add_to_wishlist",
        "remove_from_wishlist", "list_wishlist", "set_thresholds", "check_wishlist_deals",
    }
    prompts = {p.name for p in (await client.list_prompts()).prompts}
    assert prompts == {"weekly_deal_summary", "should_i_buy"}


async def test_current_deals_and_historical_low(client):
    deals = await call(client, "get_current_deals", {"game_id": HK})
    assert deals["is_on_sale"]
    assert deals["best_deal"]["shop"] == "Steam"
    assert [o["shop"] for o in deals["offers"]] == ["Steam", "GOG"]

    low = await call(client, "get_historical_low", {"game_id": HK})
    assert low["historical_low"]["price"] == 5.09
    assert low["current_best"]["shop"] == "Steam"


async def test_unknown_game_id_is_an_error_not_an_empty_result(client):
    # ITAD leaves unknown games out of its response; that must not read as "not on sale".
    assert "use search_game" in await call_error(client, "get_current_deals", {"game_id": UNKNOWN})
    assert "No price history" in await call_error(client, "get_historical_low", {"game_id": UNKNOWN})


async def test_a_game_with_null_deals_has_no_offers(client):
    deals = await call(client, "get_current_deals", {"game_id": NO_SHOPS})
    assert (deals["is_on_sale"], deals["offers"]) == (False, [])
    await call(client, "add_to_wishlist", {"game_id": NO_SHOPS, "title": "No Shops"})
    report = await call(client, "check_wishlist_deals")
    assert (report["games_checked"], report["deals"]) == (1, [])


async def test_unreadable_data_for_one_game_does_not_lose_the_others(client):
    await call(client, "add_to_wishlist", {"game_id": HK, "title": "Hollow Knight"})
    await call(client, "add_to_wishlist", {"game_id": ODD_DATA, "title": "Odd Data"})
    report = await call(client, "check_wishlist_deals")
    assert [d["title"] for d in report["deals"]] == ["Hollow Knight"]
    assert [(g["title"], "could not be read" in g["reason"]) for g in report["not_checked"]] == [("Odd Data", True)]


async def test_both_tools_agree_that_a_dearer_sale_is_not_real(client):
    # 10% off at GamesPlanet is still more than the Microsoft Store's normal price, so neither
    # tool may call it a sale; this needs prices from shops that are not discounted too.
    deals = await call(client, "get_current_deals", {"game_id": FAKE_SALE})
    assert not deals["is_on_sale"]
    await call(client, "add_to_wishlist", {"game_id": FAKE_SALE, "title": "Fake Sale"})
    assert (await call(client, "check_wishlist_deals"))["deals"] == []


# ---- Searching franchises and ambiguous titles ----


async def test_franchise_name_resolves_to_the_original_game(client, fake_itad):
    found = await call(client, "search_game", {"query": "Final Fantasy"})
    assert not found["needs_clarification"]
    assert found["results"][0] == {
        "game_id": found["results"][0]["game_id"], "title": "FINAL FANTASY", "type": "game", "match": "exact",
    }
    # More than ITAD's default of 20 results are requested, since the original game is result 24.
    assert fake_itad.calls == [("search", "Final Fantasy", 50)]


async def test_numbered_sequel_resolves_without_asking(fake_itad):
    chooser = Chooser(pick="FINAL FANTASY VIII")
    async with Client(server.mcp, elicitation_callback=chooser) as c:
        found = await call(c, "search_game", {"query": "final fantasy 7"})
    assert titles(found)[0] == "FINAL FANTASY VII"
    assert not found["needs_clarification"]
    assert chooser.asked == []


async def test_ambiguous_title_without_elicitation_asks_the_model_to_clarify(client):
    found = await call(client, "search_game", {"query": "Resident Evil 4"})
    assert found["needs_clarification"]
    assert titles(found)[:2] == ["Resident Evil 4 (2005)", "Resident Evil 4 (2023)"]


async def test_ambiguous_title_asks_the_user_through_elicitation(fake_itad):
    chooser = Chooser(pick="Resident Evil 4 (2023)")
    async with Client(server.mcp, elicitation_callback=chooser) as c:
        found = await call(c, "search_game", {"query": "Resident Evil 4"})

    message, options = chooser.asked[0]
    assert "Resident Evil 4" in message
    assert options[:2] == ["Resident Evil 4 (2005) (game)", "Resident Evil 4 (2023) (game)"]
    assert len(options) <= server.MAX_CHOICES
    assert found == {
        "query": "Resident Evil 4",
        "needs_clarification": False,
        "results": [{"game_id": found["results"][0]["game_id"], "title": "Resident Evil 4 (2023)", "type": "game", "match": "user_selected"}],
    }


async def test_franchise_without_an_exact_title_asks_the_user(fake_itad):
    chooser = Chooser(pick="Halo Infinite")
    async with Client(server.mcp, elicitation_callback=chooser) as c:
        found = await call(c, "search_game", {"query": "Halo"})
    assert titles(found) == ["Halo Infinite"]


async def test_clarified_search_only_calls_itad_once(fake_itad):
    # The question makes the client call the tool a second time with the answer, and the cached
    # search must be reused rather than fetched again.
    chooser = Chooser(pick="Resident Evil 4 (2023)")
    async with Client(server.mcp, elicitation_callback=chooser) as c:
        await call(c, "search_game", {"query": "Resident Evil 4"})
    assert [call_ for call_ in fake_itad.calls if call_[0] == "search"] == [("search", "Resident Evil 4", 50)]


async def test_games_with_the_same_title_get_distinct_visible_labels(fake_itad):
    chooser = Chooser(pick="Shadow Quest (game, shadow-quest-2009")
    async with Client(server.mcp, elicitation_callback=chooser) as c:
        found = await call(c, "search_game", {"query": "Shadow Quest"})
    options = chooser.asked[0][1]
    assert options == [
        "Shadow Quest (game, shadow-quest)",
        "Shadow Quest (game, shadow-quest-2009)",
        "Shadow Quest Remastered (game)",
    ]
    assert all(o == o.strip() for o in options)
    assert found["results"][0]["game_id"] == DUPE_B


async def test_declining_the_question_falls_back_to_the_model(fake_itad):
    chooser = Chooser(pick=None)
    async with Client(server.mcp, elicitation_callback=chooser) as c:
        found = await call(c, "search_game", {"query": "Halo"})
    assert len(chooser.asked) == 1
    assert found["needs_clarification"]
    assert len(found["results"]) == 10


async def test_browsing_never_asks(fake_itad):
    chooser = Chooser(pick="Halo Infinite")
    async with Client(server.mcp, elicitation_callback=chooser) as c:
        found = await call(c, "search_game", {"query": "Halo", "ask_if_ambiguous": False, "limit": 20})
    assert chooser.asked == []
    assert len(found["results"]) == 20


async def test_lone_partial_match_is_not_accepted_silently(client):
    # ITAD returns a single unrelated result for "Spiderman", since the games are titled "Spider-Man".
    found = await call(client, "search_game", {"query": "Spiderman"})
    assert found["needs_clarification"]
    assert titles(found) == ["Spiderman Print Studio"]


async def test_nonsense_query_is_flagged_and_not_offered_as_choices(fake_itad):
    chooser = Chooser(pick="Tomb Raider Game of the Year Edition")
    async with Client(server.mcp, elicitation_callback=chooser) as c:
        found = await call(c, "search_game", {"query": "xyzzy nonexistent game"})
    assert chooser.asked == []
    assert found["needs_clarification"]
    assert "No titles closely match" in found["note"]
    assert all(r["match"] == "weak" for r in found["results"])


async def test_search_with_no_results(client):
    found = await call(client, "search_game", {"query": "a query the fake knows nothing about"})
    assert found["needs_clarification"]
    assert found["results"] == []


async def test_a_small_limit_cannot_hide_a_second_exact_match(client):
    # Both Resident Evil 4 releases match; with limit=1 only one is returned, but it must still ask.
    found = await call(client, "search_game", {"query": "Resident Evil 4", "limit": 1})
    assert found["needs_clarification"]
    assert len(found["results"]) == 1


async def test_a_clear_match_survives_a_small_limit(client):
    found = await call(client, "search_game", {"query": "Final Fantasy", "limit": 1})
    assert not found["needs_clarification"]
    assert titles(found) == ["FINAL FANTASY"]


async def test_non_latin_queries_are_searched(client, fake_itad):
    fake_itad.search_overrides["原神"] = [{"id": HK, "title": "原神", "type": "game"}]
    found = await call(client, "search_game", {"query": "原神"})
    assert not found["needs_clarification"]
    assert found["results"][0]["match"] == "exact"


async def test_changed_results_are_asked_about_again(fake_itad, monkeypatch):
    # With the cache bypassed, the second round searches again and gets different results, as it
    # would after a very slow answer. The question then no longer matches the answer, so the user is
    # asked again with the current choices rather than having the old answer applied to new results.
    monkeypatch.setattr(server.SearchCache, "get", lambda self, query: None)
    changed = [
        {"id": HK, "title": "Resident Evil 4 (2005)", "type": "game"},
        {"id": CELESTE, "title": "Resident Evil 4 (2005)", "type": "dlc"},
    ]

    class ChangingChooser(Chooser):
        async def __call__(self, context, params):
            first_round = not self.asked
            if not first_round:
                self.pick = "Resident Evil 4 (2005) (dlc"
            answer = await super().__call__(context, params)
            fake_itad.search_overrides["Resident Evil 4"] = changed
            return answer

    chooser = ChangingChooser(pick="Resident Evil 4 (2023)")
    async with Client(server.mcp, elicitation_callback=chooser) as c:
        found = await call(c, "search_game", {"query": "Resident Evil 4"})
    assert len(chooser.asked) == 2
    assert chooser.asked[1][1] == ["Resident Evil 4 (2005) (game)", "Resident Evil 4 (2005) (dlc)"]
    assert found["results"] == [{"game_id": CELESTE, "title": "Resident Evil 4 (2005)", "type": "dlc", "match": "user_selected"}]


async def test_extra_whitespace_in_a_query_shares_one_search(client, fake_itad):
    await call(client, "search_game", {"query": "Doom"})
    await call(client, "search_game", {"query": "  Doom "})
    assert [c for c in fake_itad.calls if c[0] == "search"] == [("search", "Doom", 50)]


async def test_search_input_validation(client, fake_itad):
    for query in ("   ", "™", "!!!"):
        assert "no letters or numbers" in await call_error(client, "search_game", {"query": query})
    assert fake_itad.calls == []
    await call_error(client, "search_game", {"query": "Doom", "limit": 0})
    await call_error(client, "search_game", {"query": "Doom", "limit": 51})


# ---- Wishlist edge cases ----


async def test_add_to_wishlist_validates_its_inputs(client):
    await call_error(client, "add_to_wishlist", {"game_id": HK, "title": "Hollow Knight", "min_discount_percent": 150})
    await call_error(client, "add_to_wishlist", {"game_id": HK, "title": "Hollow Knight", "min_discount_percent": -5})
    await call_error(client, "add_to_wishlist", {"game_id": HK, "title": "Hollow Knight", "max_price": -1})
    await call_error(client, "add_to_wishlist", {"game_id": HK, "title": "Hollow Knight", "country": "USA"})
    assert await call(client, "list_wishlist") == {"result": []}


async def test_game_ids_must_be_itad_ids(client, fake_itad):
    for bad in ("Hades", "", "00000000-0000-4000-8000"):
        await call_error(client, "add_to_wishlist", {"game_id": bad, "title": "Hades"})
        await call_error(client, "get_current_deals", {"game_id": bad})
        await call_error(client, "get_historical_low", {"game_id": bad})
    assert fake_itad.calls == []
    assert await call(client, "list_wishlist") == {"result": []}


async def test_game_ids_are_trimmed_and_lowercased_everywhere(client):
    messy = f"  {HK.upper()} "
    assert (await call(client, "add_to_wishlist", {"game_id": messy, "title": "Hollow Knight"}))["game_id"] == HK
    assert (await call(client, "get_current_deals", {"game_id": messy}))["is_on_sale"]
    assert (await call(client, "get_historical_low", {"game_id": messy}))["game_id"] == HK
    assert "Removed" in (await call(client, "remove_from_wishlist", {"game_id_or_title": messy}))["result"]


async def test_a_max_price_of_zero_means_no_cap_when_adding(client):
    item = await call(client, "add_to_wishlist", {"game_id": HK, "title": "Hollow Knight", "max_price": 0})
    assert item["max_price"] is None
    assert [d["title"] for d in (await call(client, "check_wishlist_deals"))["deals"]] == ["Hollow Knight"]


async def test_country_codes_are_uppercased(client, fake_itad):
    item = await call(client, "add_to_wishlist", {"game_id": HK, "title": "Hollow Knight", "country": "gb"})
    assert item["country"] == "GB"
    await call(client, "get_current_deals", {"game_id": HK, "country": "de"})
    assert fake_itad.calls[-1][:2] == ("prices", "DE")


async def test_an_invalid_default_country_is_rejected(client, monkeypatch):
    monkeypatch.setenv("DEFAULT_COUNTRY", "usa")
    message = await call_error(client, "add_to_wishlist", {"game_id": HK, "title": "Hollow Knight"})
    assert "DEFAULT_COUNTRY" in message
    assert await call(client, "list_wishlist") == {"result": []}
    # An explicit country still works.
    await call(client, "add_to_wishlist", {"game_id": HK, "title": "Hollow Knight", "country": "US"})


async def test_default_country_comes_from_the_environment(client, monkeypatch):
    monkeypatch.setenv("DEFAULT_COUNTRY", "ca")
    item = await call(client, "add_to_wishlist", {"game_id": HK, "title": "Hollow Knight"})
    assert item["country"] == "CA"


async def test_adding_the_same_game_twice_replaces_it(client):
    await call(client, "add_to_wishlist", {"game_id": HK, "title": "Hollow Knight", "max_price": 5})
    await call(client, "add_to_wishlist", {"game_id": HK, "title": "Hollow Knight", "max_price": 3})
    items = (await call(client, "list_wishlist"))["result"]
    assert [(i["game_id"], i["max_price"]) for i in items] == [(HK, 3.0)]


async def test_shared_title_is_ambiguous_for_remove_and_thresholds(client):
    await call(client, "add_to_wishlist", {"game_id": DOOM_1993, "title": "DOOM"})
    await call(client, "add_to_wishlist", {"game_id": DOOM_2016, "title": "Doom"})

    message = await call_error(client, "remove_from_wishlist", {"game_id_or_title": "doom"})
    assert DOOM_1993 in message and DOOM_2016 in message
    await call_error(client, "set_thresholds", {"game_id_or_title": "doom", "max_price": 5})

    assert "Removed 'Doom'" in (await call(client, "remove_from_wishlist", {"game_id_or_title": DOOM_2016}))["result"]
    # With only one left, the title is no longer ambiguous.
    await call(client, "set_thresholds", {"game_id_or_title": "doom", "max_price": 5})


async def test_a_title_that_looks_like_an_id_does_not_hide_the_real_id(client):
    await call(client, "add_to_wishlist", {"game_id": HK, "title": "Hollow Knight"})
    await call(client, "add_to_wishlist", {"game_id": OTHER, "title": HK})
    # HK is an id first, so it removes Hollow Knight rather than reporting an ambiguous match.
    assert "Removed 'Hollow Knight'" in (await call(client, "remove_from_wishlist", {"game_id_or_title": HK}))["result"]
    # With no id HK left, the same text now matches the other game by its title.
    assert f"Removed '{HK}'" in (await call(client, "remove_from_wishlist", {"game_id_or_title": HK}))["result"]


async def test_a_game_removed_meanwhile_is_reported(client, monkeypatch):
    # Another call removes the game between the lookup and the change.
    await call(client, "add_to_wishlist", {"game_id": HK, "title": "Hollow Knight"})

    async def gone(*args, **kwargs):
        return None

    async def nothing_removed(*args, **kwargs):
        return False

    monkeypatch.setattr(server.db, "set_thresholds", gone)
    monkeypatch.setattr(server.db, "remove_item", nothing_removed)
    assert "not on the wishlist" in await call_error(client, "set_thresholds", {"game_id_or_title": HK, "max_price": 5})
    assert "not on the wishlist" in await call_error(client, "remove_from_wishlist", {"game_id_or_title": HK})


async def test_titles_are_matched_loosely_when_removing(client):
    await call(client, "add_to_wishlist", {"game_id": HK, "title": "Ōkami HD "})
    assert "Removed 'Ōkami HD'" in (await call(client, "remove_from_wishlist", {"game_id_or_title": "okami  hd"}))["result"]


async def test_missing_games_give_clear_errors(client):
    assert "not on the wishlist" in await call_error(client, "remove_from_wishlist", {"game_id_or_title": "nope"})
    assert "not on the wishlist" in await call_error(client, "set_thresholds", {"game_id_or_title": "nope", "max_price": 1})


async def test_set_thresholds_changes_only_what_is_given(client):
    await call(client, "add_to_wishlist", {"game_id": HK, "title": "Hollow Knight", "max_price": 10})
    item = await call(client, "set_thresholds", {"game_id_or_title": "Hollow Knight", "min_discount_percent": 40})
    assert (item["min_discount_percent"], item["max_price"]) == (40, 10.0)
    await call_error(client, "set_thresholds", {"game_id_or_title": "Hollow Knight", "min_discount_percent": 101})


# ---- Deal check ----


async def test_empty_wishlist_makes_no_api_calls(client, fake_itad):
    assert await call(client, "check_wishlist_deals") == {"games_checked": 0, "games_on_sale": 0, "deals": []}
    assert fake_itad.calls == []


async def test_deal_check_applies_thresholds(client):
    await call(client, "add_to_wishlist", {"game_id": HK, "title": "Hollow Knight"})
    await call(client, "add_to_wishlist", {"game_id": CELESTE, "title": "Celeste", "max_price": 3.0})
    await call(client, "add_to_wishlist", {"game_id": FULL_PRICE, "title": "Full Price Game"})

    report = await call(client, "check_wishlist_deals")
    assert report["games_checked"] == 3
    # Celeste is over its max price and the full-price game is not on sale.
    assert [d["title"] for d in report["deals"]] == ["Hollow Knight"]

    await call(client, "set_thresholds", {"game_id_or_title": CELESTE, "max_price": 0})
    report = await call(client, "check_wishlist_deals")
    assert [d["title"] for d in report["deals"]] == ["Celeste", "Hollow Knight"]


async def test_deal_check_batches_one_request_per_country(client, fake_itad):
    await call(client, "add_to_wishlist", {"game_id": HK, "title": "Hollow Knight", "country": "US"})
    await call(client, "add_to_wishlist", {"game_id": CELESTE, "title": "Celeste", "country": "US"})
    await call(client, "add_to_wishlist", {"game_id": FULL_PRICE, "title": "Full Price Game", "country": "GB"})
    await call(client, "check_wishlist_deals")
    assert sorted(c for c in fake_itad.calls if c[0] == "prices") == [
        ("prices", "GB", [FULL_PRICE]),
        ("prices", "US", sorted([CELESTE, HK])),
    ]


async def test_games_missing_from_itad_are_reported_and_not_counted(client):
    await call(client, "add_to_wishlist", {"game_id": HK, "title": "Hollow Knight"})
    await call(client, "add_to_wishlist", {"game_id": DELISTED, "title": "Delisted Game"})
    report = await call(client, "check_wishlist_deals")
    assert report["games_checked"] == 1
    assert [d["title"] for d in report["deals"]] == ["Hollow Knight"]
    assert report["not_checked"] == [{
        "game_id": DELISTED, "title": "Delisted Game", "country": "US",
        "reason": "IsThereAnyDeal no longer knows this id; search for the game again and re-add it.",
    }]


async def test_invalid_stored_id_does_not_fail_the_whole_batch(client, fake_itad):
    # An id stored by an older version of the server, which never checked the format.
    await insert_legacy_row("Hades", "Hades")
    await call(client, "add_to_wishlist", {"game_id": HK, "title": "Hollow Knight"})
    report = await call(client, "check_wishlist_deals")
    assert [d["title"] for d in report["deals"]] == ["Hollow Knight"]
    assert [(g["game_id"], g["title"]) for g in report["not_checked"]] == [("Hades", "Hades")]
    assert [c for c in fake_itad.calls if c[0] == "prices"] == [("prices", "US", [HK])]


async def test_stored_id_with_a_trailing_newline_is_not_sent(client, fake_itad):
    await insert_legacy_row(CELESTE + "\n", "Celeste")
    await call(client, "add_to_wishlist", {"game_id": HK, "title": "Hollow Knight"})
    report = await call(client, "check_wishlist_deals")
    assert [g["title"] for g in report["not_checked"]] == ["Celeste"]
    assert [c for c in fake_itad.calls if c[0] == "prices"] == [("prices", "US", [HK])]


async def test_skipped_games_are_still_reported_when_every_batch_fails(client, fake_itad):
    fake_itad.failing_countries = {"US"}
    await insert_legacy_row("Hades", "Hades")
    await call(client, "add_to_wishlist", {"game_id": HK, "title": "Hollow Knight"})
    report = await call(client, "check_wishlist_deals")
    assert report["games_checked"] == 0
    assert [(g["title"], g["reason"][:20]) for g in report["not_checked"]] == [
        ("Hades", "This is not a valid "),
        ("Hollow Knight", "Prices are unavailab"),
    ]


async def test_price_requests_run_a_few_at_a_time(client, fake_itad, monkeypatch):
    monkeypatch.setattr(server.deal_check, "PRICES_BATCH_SIZE", 1)
    fake_itad.price_delay = 0.02
    for game_id in (HK, CELESTE, FULL_PRICE, FAKE_SALE, MARKET, DELISTED):
        await call(client, "add_to_wishlist", {"game_id": game_id, "title": game_id})
    await call(client, "check_wishlist_deals")
    assert len([c for c in fake_itad.calls if c[0] == "prices"]) == 6
    assert 1 < fake_itad.max_in_flight <= server.deal_check.PRICES_CONCURRENCY


async def test_large_wishlists_are_split_into_batches(client, fake_itad, monkeypatch):
    monkeypatch.setattr(server.deal_check, "PRICES_BATCH_SIZE", 2)
    for game_id in (HK, CELESTE, FULL_PRICE, FAKE_SALE, MARKET):
        await call(client, "add_to_wishlist", {"game_id": game_id, "title": game_id})
    report = await call(client, "check_wishlist_deals")
    batches = [c[2] for c in fake_itad.calls if c[0] == "prices"]
    assert [len(b) for b in batches] == [2, 2, 1]
    assert sorted(sum(batches, [])) == sorted([HK, CELESTE, FULL_PRICE, FAKE_SALE, MARKET])
    assert report["games_checked"] == 5


async def test_minimum_discount_is_measured_against_the_market_price(client):
    await call(client, "add_to_wishlist", {"game_id": MARKET, "title": "Market Game", "min_discount_percent": 40})
    assert (await call(client, "check_wishlist_deals"))["deals"] == []

    await call(client, "set_thresholds", {"game_id_or_title": MARKET, "min_discount_percent": 14})
    deal = (await call(client, "check_wishlist_deals"))["deals"][0]
    assert (deal["discount_percent"], deal["market_discount_percent"]) == (50, 14)


async def test_one_failing_country_does_not_hide_other_deals(client, fake_itad):
    fake_itad.failing_countries = {"GB"}
    await call(client, "add_to_wishlist", {"game_id": HK, "title": "Hollow Knight", "country": "US"})
    await call(client, "add_to_wishlist", {"game_id": CELESTE, "title": "Celeste", "country": "GB"})
    report = await call(client, "check_wishlist_deals")
    assert [d["title"] for d in report["deals"]] == ["Hollow Knight"]
    assert report["games_checked"] == 1
    assert report["not_checked"] == [
        {"game_id": CELESTE, "title": "Celeste", "country": "GB", "reason": "Prices are unavailable for GB."}
    ]


async def test_every_country_failing_is_a_tool_error(client, fake_itad):
    fake_itad.failing_countries = {"US"}
    await call(client, "add_to_wishlist", {"game_id": HK, "title": "Hollow Knight"})
    assert "Prices are unavailable for US" in await call_error(client, "check_wishlist_deals")


# ---- Resources and prompts ----


async def test_resources(client):
    await call(client, "add_to_wishlist", {"game_id": HK, "title": "Hollow Knight"})

    items = json.loads((await client.read_resource("wishlist://items")).contents[0].text)
    assert items[0]["game_title"] == "Hollow Knight"

    item = json.loads((await client.read_resource(f"wishlist://items/{HK}")).contents[0].text)
    assert item["game_id"] == HK


async def test_missing_resource_is_an_error(client):
    with pytest.raises(Exception, match="No wishlist game"):
        await client.read_resource("wishlist://items/nope")


async def test_item_resource_only_matches_ids(client):
    await call(client, "add_to_wishlist", {"game_id": HK, "title": "Hollow Knight"})
    with pytest.raises(Exception, match="No wishlist game"):
        await client.read_resource("wishlist://items/Hollow Knight")


async def test_prompts(client):
    prompt = await client.get_prompt("should_i_buy", {"title": "Celeste"})
    assert "Celeste" in prompt.messages[0].content.text
    prompt = await client.get_prompt("weekly_deal_summary")
    assert "check_wishlist_deals" in prompt.messages[0].content.text


# ---- Search cache ----


def test_search_cache_expires_entries(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: now[0])
    cache = server.SearchCache(ttl_seconds=60)
    cache.put("Doom", [{"id": "1"}])
    assert cache.get("DOOM") == [{"id": "1"}]
    now[0] += 61
    assert cache.get("Doom") is None


def test_re_caching_a_query_does_not_evict_another_entry(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: now[0])
    cache = server.SearchCache(ttl_seconds=60, max_entries=2)
    cache.put("pending", [{"id": "p"}])
    cache.put("other", [{"id": "o"}])
    # Re-caching a query that is already cached replaces it rather than evicting "pending".
    cache.put("other", [{"id": "o2"}])
    assert cache.get("pending") == [{"id": "p"}]
    # Expired entries are cleared first, so a new query does not push out a live one.
    now[0] += 30
    cache.put("pending", [{"id": "p2"}])
    now[0] += 40
    cache.put("new", [{"id": "n"}])
    assert cache.get("pending") == [{"id": "p2"}]
    assert cache.get("new") == [{"id": "n"}]


def test_search_cache_drops_the_oldest_entry_when_full():
    cache = server.SearchCache(max_entries=2)
    for query in ("a", "b", "c"):
        cache.put(query, [{"id": query}])
    assert cache.get("a") is None
    assert cache.get("b") == [{"id": "b"}]
    assert cache.get("c") == [{"id": "c"}]


# ---- Configuration ----


def test_env_file_candidates(monkeypatch, tmp_path):
    monkeypatch.setenv("DEALS_ENV_FILE", str(tmp_path / "custom.env"))
    candidates = config.env_file_candidates()
    assert candidates[0] == tmp_path / "custom.env"
    assert candidates[-1] == Path.home() / ".deals-mcp" / ".env"
    monkeypatch.delenv("DEALS_ENV_FILE")
    assert len(config.env_file_candidates()) == 2
