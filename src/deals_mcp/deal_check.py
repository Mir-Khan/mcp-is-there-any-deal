"""Checks a whole wishlist against current ITAD prices; shared by the MCP tool and the alert job."""

import asyncio
import logging
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from deals_mcp import db
from deals_mcp.deal_logic import best_deal, market_msrp, passes_thresholds, summarize_deal, usable_deals
from deals_mcp.itad import ITADClient

logger = logging.getLogger(__name__)

# The most ids sent in one prices request, and the most requests at once, so that a large wishlist
# stays within ITAD's limits.
PRICES_BATCH_SIZE = 100
PRICES_CONCURRENCY = 4


async def check_wishlist(itad: ITADClient, items: list[dict]) -> dict[str, Any]:
    """Find the wishlist games that are on a real sale and meet their thresholds.

    Returns {games_checked, games_on_sale, deals, not_checked?}, where deals are sorted by discount
    against the lowest regular price across shops, and not_checked lists the games that could not be
    checked along with the reason. Raises ToolError only when every request failed and there is
    nothing else to report.
    """
    if not items:
        return {"games_checked": 0, "games_on_sale": 0, "deals": []}

    not_checked = []

    def skip(item, reason):
        not_checked.append({"game_id": item["game_id"], "title": item["game_title"], "country": item["country"], "reason": reason})

    # Group by country, and split each country into batches; ids that cannot be ITAD ids (from an
    # older version of this server) are skipped, since ITAD rejects a whole batch for one bad id.
    by_country: dict[str, dict[str, dict]] = {}
    for item in items:
        if db.is_itad_id(item["game_id"]):
            by_country.setdefault(item["country"], {})[item["game_id"]] = item
        else:
            skip(item, "This is not a valid IsThereAnyDeal id; search for the game again and re-add it.")
    batches = []
    for country, games in by_country.items():
        entries = list(games.items())
        for i in range(0, len(entries), PRICES_BATCH_SIZE):
            batches.append((country, dict(entries[i : i + PRICES_BATCH_SIZE])))

    # A few requests at a time, so that a large wishlist does not trip ITAD's rate limit.
    limiter = asyncio.Semaphore(PRICES_CONCURRENCY)

    async def fetch(country, games):
        async with limiter:
            return await itad.prices(list(games), country)

    responses = await asyncio.gather(*(fetch(country, games) for country, games in batches), return_exceptions=True)

    # Cancellation and similar signals are not failures of a batch, so they are passed on as they are.
    for response in responses:
        if isinstance(response, BaseException) and not isinstance(response, Exception):
            raise response
    # A failed batch should not hide the deals found in the others, unless every batch failed.
    # When games were also skipped for other reasons, the report is returned instead, so that those
    # reasons are not lost behind the error.
    errors = [r for r in responses if isinstance(r, Exception)]
    if errors and len(errors) == len(responses) and not not_checked:
        if isinstance(errors[0], ToolError):
            raise errors[0]
        raise ToolError("Could not fetch prices from IsThereAnyDeal.") from errors[0]

    on_sale, checked = [], 0
    for (country, games), results in zip(batches, responses):
        if isinstance(results, Exception):
            if isinstance(results, ToolError):
                message = str(results)
                logger.error("Price check failed for %s: %s", country, message)
            else:
                # Only the type is logged for unexpected errors, since some include the request URL.
                message = "Could not fetch prices."
                logger.error("Price check failed for %s: %s", country, type(results).__name__)
            for item in games.values():
                skip(item, message)
            continue

        by_id = {result.get("id"): result for result in results}
        for game_id, item in games.items():
            result = by_id.get(game_id)
            if result is None:
                # ITAD leaves out ids it does not know, such as one that was merged or removed.
                skip(item, "IsThereAnyDeal no longer knows this id; search for the game again and re-add it.")
                continue
            try:
                deals = usable_deals(result.get("deals"))
                deal = best_deal(deals)
                summary = summarize_deal(deal, market_msrp(deals)) if deal else None
            except Exception:
                logger.exception("Could not read the prices for %s", game_id)
                skip(item, "IsThereAnyDeal returned price data for this game that could not be read.")
                continue
            checked += 1
            if not summary:
                continue
            if not passes_thresholds(summary["price"], summary["market_discount_percent"], item["min_discount_percent"], item["max_price"]):
                continue
            on_sale.append({"game_id": game_id, "title": item["game_title"], "country": country, **summary})

    on_sale.sort(key=lambda d: d["market_discount_percent"], reverse=True)
    report = {"games_checked": checked, "games_on_sale": len(on_sale), "deals": on_sale}
    if not_checked:
        report["not_checked"] = not_checked
    return report
