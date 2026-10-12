"""MCP server that exposes PC game deals (from IsThereAnyDeal) and a local wishlist."""

import json
import logging
import os
import sys
import time
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any, Literal

import aiohttp
from mcp.server.mcpserver import AcceptedElicitation, Context, Elicit, ElicitationResult, MCPServer, Resolve
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from mcp_types import ToolAnnotations
from pydantic import BeforeValidator, Field, create_model

from deals_mcp import db, deal_check
from deals_mcp.config import get_country, get_db_path, load_env
from deals_mcp.deal_logic import best_deal, market_msrp, summarize_deal, usable_deals
from deals_mcp.itad import DEFAULT_TIMEOUT, ITADClient
from deals_mcp.matching import normalize, rank_results

# Logging has to go to stderr, because stdout carries the stdio transport's JSON-RPC messages.
logging.basicConfig(level=logging.INFO, format="%(asctime)s:%(levelname)s:%(name)s: %(message)s", stream=sys.stderr)
logger = logging.getLogger("deals_mcp")

load_env()


# Shared argument types, so that bad values are rejected before a tool runs.
ITAD_ID_PATTERN = f"^{db.ITAD_ID.pattern}$"
# Ids are trimmed and lowercased first (in the same way as the database does), and must then look
# like an ITAD id, so that a title passed by mistake is rejected here rather than being stored and
# later failing a whole batched price request.
GameId = Annotated[
    str,
    BeforeValidator(lambda v: db.normalize_id(v) if isinstance(v, str) else v),
    Field(pattern=ITAD_ID_PATTERN, description="An IsThereAnyDeal game id (a UUID from search_game), not a title."),
]
Country = Annotated[str, Field(pattern=r"^[A-Za-z]{2}$", description="Two-letter country code, e.g. US or GB.")]
DiscountPercent = Annotated[int, Field(ge=0, le=100)]
Price = Annotated[float, Field(ge=0)]

# The most choices to show in a clarification form, since a longer list is hard to pick from.
MAX_CHOICES = 8


class SearchCache:
    """Keeps recent ITAD search results for a few minutes.

    Under the 2026-07-28 protocol, a tool that asks the user a question is called again once the
    user answers, and its resolvers run again; without this cache every clarified search would hit
    ITAD twice, and a change in ITAD's results between the two calls would discard the answer.
    """

    def __init__(self, ttl_seconds: float = 900, max_entries: int = 64):
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._entries: dict[str, tuple[float, list[dict]]] = {}

    def get(self, query: str) -> list[dict] | None:
        entry = self._entries.get(query.casefold())
        if entry and time.monotonic() - entry[0] < self.ttl_seconds:
            return entry[1]
        return None

    def put(self, query: str, results: list[dict]) -> None:
        key = query.casefold()
        now = time.monotonic()
        # Re-caching a query replaces its entry (and moves it to the end) instead of evicting another.
        self._entries.pop(key, None)
        for stale in [k for k, (stored, _) in self._entries.items() if now - stored >= self.ttl_seconds]:
            del self._entries[stale]
        if len(self._entries) >= self.max_entries:
            # Drop the oldest live entry; dicts keep insertion order.
            self._entries.pop(next(iter(self._entries)))
        self._entries[key] = (now, results)


@dataclass
class AppState:
    itad: ITADClient
    search_cache: SearchCache


@asynccontextmanager
async def lifespan(server: MCPServer) -> AsyncIterator[AppState]:
    await db.init_db(get_db_path())
    async with aiohttp.ClientSession(timeout=DEFAULT_TIMEOUT) as session:
        yield AppState(itad=ITADClient(session, os.getenv("ITAD_API_KEY")), search_cache=SearchCache())


mcp = MCPServer(
    name="deals",
    instructions=(
        "Tools for PC game prices from IsThereAnyDeal and a local wishlist. Game ids are ITAD ids, "
        "so use search_game to find the id of a title before calling the other tools."
    ),
    lifespan=lifespan,
)

READ_ONLY_WEB = ToolAnnotations(read_only_hint=True, open_world_hint=True)


def state(ctx: Context) -> AppState:
    return ctx.request_context.lifespan_context


def itad(ctx: Context) -> ITADClient:
    return state(ctx).itad


# ---- Price lookups ----


NO_CLOSE_MATCH_NOTE = (
    "No titles closely match this query, so any results are only loose guesses from IsThereAnyDeal. "
    "Try the full official title or another spelling, such as Spider-Man."
)


async def run_search(query: str, limit: int, ctx: Context) -> dict[str, Any]:
    """Search ITAD and rank the results; this runs before search_game's body, as a resolver.

    The status is "clear" for a single exact match, "ambiguous" when there are close matches to
    choose from, and "no_close_match" otherwise.
    """
    # Extra whitespace is removed before searching, so "Doom " and "Doom" share one cached search.
    query = " ".join(query.split())
    if not normalize(query):
        raise ToolError("The search query has no letters or numbers in it.")
    cache = state(ctx).search_cache
    raw = cache.get(query)
    if raw is None:
        raw = await itad(ctx).search(query)
        cache.put(query, raw)

    ranked = rank_results(query, raw)
    everything = [{"game_id": r["id"], "title": r["title"], "type": r.get("type"), "match": level} for r, level in ranked]
    # The slug is only used to tell apart choices that share a title, so it is kept out of the results.
    slugs = {r["id"]: r.get("slug") for r, _ in ranked}

    # The status is decided on every result before the limit is applied, so that a small limit
    # cannot hide a second exact match and make the search look certain.
    exact = [r for r in everything if r["match"] == "exact"]
    close = [r for r in everything if r["match"] != "weak"]
    # Only a single exact match is taken as certain; a lone partial match still needs confirming,
    # since ITAD may have missed the real game (it finds nothing useful for "Spiderman", for example).
    if len(exact) == 1:
        status, choices = "clear", []
    elif close:
        status, choices = "ambiguous", close[:MAX_CHOICES]
    else:
        status, choices = "no_close_match", []
    # Exact matches always rank first, so a clear match is always inside the limit.
    return {"status": status, "results": everything[:limit], "labels": choice_labels(choices, slugs)}


def choice_labels(choices: list[dict], slugs: dict[str, str | None]) -> dict[str, dict]:
    """Give every choice a distinct, readable label for the elicitation form.

    Games that share a title and type also show their ITAD slug (or the start of their id), so the
    user can tell them apart and no label depends on invisible differences such as whitespace.
    """
    def label(r, detail=None):
        kind = r["type"] or "other"
        return f"{r['title']} ({kind}, {detail})" if detail else f"{r['title']} ({kind})"

    counts = Counter(label(r) for r in choices)

    labels = {}
    for r in choices:
        text = label(r)
        if counts[text] > 1:
            text = label(r, slugs.get(r["game_id"]) or r["game_id"][:8])
        if text in labels:
            text = label(r, r["game_id"])
        labels[text] = r
    return labels


async def ask_which_game(
    query: str,
    ask_if_ambiguous: bool,
    search: Annotated[dict[str, Any], Resolve(run_search)],
    ctx: Context,
) -> Elicit[Any] | None:
    """Ask the user to pick a game through MCP elicitation, when the search is ambiguous.

    Returns None (so that nothing is asked) when the search is clear, when there are no close
    matches to offer, when the caller only wants to browse, or when the client cannot show forms;
    in those cases search_game reports needs_clarification and the model asks in the conversation.
    """
    capabilities = ctx.client_capabilities
    if search["status"] != "ambiguous" or not ask_if_ambiguous or not capabilities or not capabilities.elicitation:
        return None
    choice_model = create_model(
        "GameChoice",
        game=(Literal[tuple(search["labels"])], Field(description="The game you meant")),
    )
    return Elicit(f'Several games match "{query}". Which one did you mean?', choice_model)


@mcp.tool(annotations=READ_ONLY_WEB)
async def search_game(
    query: str,
    search: Annotated[dict[str, Any], Resolve(run_search)],
    choice: Annotated[ElicitationResult[Any], Resolve(ask_which_game)],
    limit: Annotated[int, Field(ge=1, le=50)] = 10,
    ask_if_ambiguous: bool = True,
) -> dict[str, Any]:
    """Search IsThereAnyDeal for PC games by title, ranked by how closely each title matches.

    Each result has a "match" level: exact, variant (exact apart from a suffix such as a year),
    starts_with, contains, or weak. A single exact match, such as "FINAL FANTASY" for the query
    "Final Fantasy", is the game the user means, so use it and not a sequel or remake.

    When the search is ambiguous and ask_if_ambiguous is true, the user may be asked to pick a game
    directly, and then only that game is returned with match "user_selected". If needs_clarification
    is true in the result, ask the user which game they mean before acting on any of them. Set
    ask_if_ambiguous to false when the user only wants to browse a list of games.
    """
    results = search["results"]
    if isinstance(choice, AcceptedElicitation) and choice.data is not None:
        chosen = search["labels"].get(choice.data.game)
        if chosen:
            return {"query": query, "needs_clarification": False, "results": [{**chosen, "match": "user_selected"}]}
        # The search was run again (for example after a very slow answer) and no longer offers the
        # chosen game, so the answer cannot be applied; say so rather than silently asking again.
        return {
            "query": query,
            "needs_clarification": True,
            "note": f"The search results changed before the answer '{choice.data.game}' arrived; ask the user again.",
            "results": results,
        }

    if search["status"] == "clear":
        return {"query": query, "needs_clarification": False, "results": results}
    if search["status"] == "no_close_match":
        return {"query": query, "needs_clarification": True, "note": NO_CLOSE_MATCH_NOTE, "results": results}
    return {"query": query, "needs_clarification": True, "results": results}


@mcp.tool(annotations=READ_ONLY_WEB)
async def get_current_deals(game_id: GameId, ctx: Context, country: Country | None = None) -> dict[str, Any]:
    """Get the current price at every shop for one game, sorted from cheapest, and whether the
    cheapest offer is a real sale (below the lowest regular price across all shops). The best deal's
    market_discount_percent is its discount against that lowest regular price."""
    country = get_country(country)
    results = await itad(ctx).prices([game_id], country)
    # ITAD leaves out games it does not know, so an empty response means a wrong or stale id,
    # which is reported as an error rather than as "no offers".
    if not results:
        raise ToolError(f"IsThereAnyDeal has no game with id {game_id}; use search_game to find the id.")
    deals = usable_deals(results[0].get("deals"))
    best = best_deal(deals)
    return {
        "game_id": game_id,
        "country": country,
        "is_on_sale": best is not None,
        "best_deal": summarize_deal(best, market_msrp(deals)) if best else None,
        "offers": [summarize_deal(d) for d in sorted(deals, key=lambda d: d["price"]["amount"])],
    }


@mcp.tool(annotations=READ_ONLY_WEB)
async def get_historical_low(game_id: GameId, ctx: Context, country: Country | None = None) -> dict[str, Any]:
    """Get the all-time lowest recorded price for a game, along with the current best price."""
    country = get_country(country)
    overview = await itad(ctx).overview([game_id], country)
    prices = overview.get("prices") or []
    if not prices:
        raise ToolError(f"No price history was found for game id {game_id} in {country}.")

    def flatten(entry):
        if not entry:
            return None
        return {
            "shop": entry["shop"]["name"],
            "price": entry["price"]["amount"],
            "currency": entry["price"]["currency"],
            "discount_percent": entry.get("cut"),
            "timestamp": entry.get("timestamp"),
        }

    game = prices[0]
    return {
        "game_id": game_id,
        "country": country,
        "historical_low": flatten(game.get("lowest")),
        "current_best": flatten(game.get("current")),
        "bundled_count": game.get("bundled", 0),
    }


# ---- Wishlist management ----


@mcp.tool()
async def add_to_wishlist(
    game_id: GameId,
    title: str,
    country: Country | None = None,
    min_discount_percent: DiscountPercent = 0,
    max_price: Price | None = None,
) -> dict[str, Any]:
    """Add a game to the wishlist (or replace its entry). Use search_game first to get the ITAD
    game id. Optionally only report deals with at least min_discount_percent off the lowest regular
    price across shops, or at or below max_price; a max_price of 0 means no price cap."""
    return await db.add_item(get_db_path(), game_id, title, get_country(country), min_discount_percent, max_price)


async def find_one_item(game_id_or_title: str) -> dict:
    """Find exactly one wishlist item by id or title, or raise an error that the model can act on."""
    items = await db.find_by_id_or_title(get_db_path(), game_id_or_title)
    if not items:
        raise ToolError(f"'{game_id_or_title}' is not on the wishlist.")
    if len(items) > 1:
        ids = ", ".join(f"{i['game_title']} ({i['game_id']})" for i in items)
        raise ToolError(f"'{game_id_or_title}' matches more than one wishlist game, so use the game id instead: {ids}")
    return items[0]


@mcp.tool(annotations=ToolAnnotations(destructive_hint=True))
async def remove_from_wishlist(game_id_or_title: str) -> str:
    """Remove a game from the wishlist, matching by ITAD id or exact title (case-insensitive)."""
    item = await find_one_item(game_id_or_title)
    if not await db.remove_item(get_db_path(), item["game_id"]):
        raise ToolError(f"'{game_id_or_title}' is not on the wishlist.")
    return f"Removed '{item['game_title']}' from the wishlist."


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
async def list_wishlist() -> list[dict[str, Any]]:
    """List every game on the wishlist along with its country and deal thresholds."""
    return await db.list_items(get_db_path())


@mcp.tool()
async def set_thresholds(
    game_id_or_title: str,
    min_discount_percent: DiscountPercent | None = None,
    max_price: Price | None = None,
) -> dict[str, Any]:
    """Change the deal thresholds of a wishlist game; only the values given are changed. The
    minimum discount is measured against the lowest regular price across shops. Set max_price to 0
    in order to remove the price cap."""
    item = await find_one_item(game_id_or_title)
    updated = await db.set_thresholds(get_db_path(), item["game_id"], min_discount_percent, max_price)
    if updated is None:
        raise ToolError(f"'{game_id_or_title}' is not on the wishlist.")
    return updated


# ---- Deal check ----


@mcp.tool(annotations=READ_ONLY_WEB)
async def check_wishlist_deals(ctx: Context) -> dict[str, Any]:
    """Check every wishlist game for a current sale that is a real discount and that meets the
    game's thresholds, and return the games that are on sale, sorted by discount against the lowest
    regular price across shops. Games that could not be checked are listed under not_checked with
    the reason, and are not counted in games_checked, so never describe them as "not on sale"."""
    return await deal_check.check_wishlist(itad(ctx), await db.list_items(get_db_path()))


# ---- Resources ----


@mcp.resource("wishlist://items", mime_type="application/json", description="Every game on the wishlist.")
async def wishlist_resource() -> str:
    return json.dumps(await db.list_items(get_db_path()), indent=2)


@mcp.resource("wishlist://items/{game_id}", mime_type="application/json", description="One wishlist game by ITAD id.")
async def wishlist_item_resource(game_id: str) -> str:
    item = await db.get_item(get_db_path(), game_id)
    if item is None:
        raise ResourceNotFoundError(f"No wishlist game with id {game_id}.")
    return json.dumps(item, indent=2)


# ---- Prompts ----


@mcp.prompt()
def weekly_deal_summary() -> str:
    """Summarize which wishlist games are worth buying this week."""
    return (
        "Run check_wishlist_deals and write a short summary of the deals on my wishlist. Rank them "
        "from most to least worth buying, and for the top few call get_historical_low so that you can "
        "say how close the current price is to the all-time low. Keep it to a few lines per game, and "
        "if nothing is on sale, say so plainly."
    )


@mcp.prompt()
def should_i_buy(title: str) -> str:
    """Decide whether a game is worth buying now or whether it is better to wait for a sale."""
    return (
        f"I am thinking about buying {title} on PC. Use search_game to find it, then get_current_deals "
        "and get_historical_low, and tell me whether the current best price is a good one compared "
        "with the historical low. Give a clear recommendation, namely buy now or wait, and the reason."
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
