"""Pure functions that decide what counts as a deal, extracted from discord-deal-bot's alert code."""

import math


def usable_deals(deals: list[dict] | None) -> list[dict]:
    """The deals that have a numeric price; ITAD can send null fields, so nothing else is assumed."""
    return [d for d in deals or [] if isinstance((d.get("price") or {}).get("amount"), (int, float))]


def _regular(deal: dict) -> float | None:
    amount = (deal.get("regular") or {}).get("amount")
    return amount if isinstance(amount, (int, float)) else None


def market_msrp(deals: list[dict]) -> float | None:
    """The "market MSRP": the lowest regular price across all shops, or None if no shop lists one.

    A regular price of 0 (a giveaway or a bad listing) is ignored, since it would make every real
    sale look like no discount at all.
    """
    return min((r for d in usable_deals(deals) if (r := _regular(d)) and r > 0), default=None)


def best_deal(deals: list[dict]) -> dict | None:
    """Return the cheapest ITAD deal if it is a real sale, otherwise None.

    A deal only counts when its price is below the market MSRP, which filters out "sales" that
    still cost more than the normal price somewhere else.
    """
    deals = usable_deals(deals)
    if not deals:
        return None
    cheapest = min(deals, key=lambda d: d["price"]["amount"])
    msrp = market_msrp(deals)
    # Without any regular price there is nothing to compare against, so it cannot be called a sale.
    if msrp is None or cheapest["price"]["amount"] >= msrp:
        return None
    return cheapest


def market_discount_percent(price: float, msrp: float | None) -> int:
    """The discount against the market MSRP, rounded down so that 39.9% never passes a 40% minimum.

    This differs from a shop's own "cut", which is measured against that shop's regular price:
    $30 at a shop whose regular price is $60 is a 50% cut, but only 14% off when another shop's
    regular price is $35.
    """
    if not msrp or price >= msrp:
        return 0
    return math.floor((1 - price / msrp) * 100 + 1e-9)


def passes_thresholds(price: float, discount: int, min_discount: int | None, max_price: float | None) -> bool:
    """Check a deal against a wishlist item's minimum discount and maximum price."""
    if min_discount and discount < min_discount:
        return False
    if max_price is not None and price > max_price:
        return False
    return True


def summarize_deal(deal: dict, msrp: float | None = None) -> dict:
    """Flatten an ITAD deal into the fields that are useful to show to a model."""
    summary = {
        "shop": (deal.get("shop") or {}).get("name"),
        "price": deal["price"]["amount"],
        "regular_price": _regular(deal),
        "currency": deal["price"].get("currency"),
        "discount_percent": deal.get("cut") or 0,
        "url": deal.get("url"),
        "expiry": deal.get("expiry"),
        "drm": [d.get("name") for d in deal.get("drm") or [] if d],
    }
    if msrp is not None:
        summary["market_discount_percent"] = market_discount_percent(deal["price"]["amount"], msrp)
    return summary
