from deals_mcp.deal_logic import (
    best_deal,
    market_discount_percent,
    market_msrp,
    passes_thresholds,
    summarize_deal,
    usable_deals,
)


def make_deal(shop, price, regular, cut=0):
    return {
        "shop": {"name": shop},
        "price": {"amount": price, "currency": "USD"},
        "regular": {"amount": regular, "currency": "USD"},
        "cut": cut,
        "url": f"https://example.com/{shop}",
        "drm": [{"name": "Steam"}],
    }


def test_no_deals_returns_none():
    assert best_deal([]) is None


def test_real_sale_is_returned():
    deals = [make_deal("Steam", 7.49, 14.99, 50), make_deal("GOG", 14.99, 14.99)]
    assert best_deal(deals)["shop"]["name"] == "Steam"


def test_sale_above_another_shops_regular_price_is_rejected():
    # A "50% off" sale from an inflated regular price still costs more than GOG's normal price.
    deals = [make_deal("Overpriced", 15.00, 30.00, 50), make_deal("GOG", 14.99, 14.99)]
    assert best_deal(deals) is None


def test_cheapest_deal_is_picked_regardless_of_order():
    deals = [make_deal("A", 10.00, 20.00, 50), make_deal("B", 5.00, 20.00, 75)]
    assert best_deal(deals)["shop"]["name"] == "B"


def test_thresholds():
    assert passes_thresholds(10.0, 50, 0, None)
    assert not passes_thresholds(10.0, 40, 50, None)
    assert passes_thresholds(10.0, 50, 50, None)
    assert not passes_thresholds(10.0, 50, 0, 9.99)
    assert passes_thresholds(10.0, 50, 0, 10.0)


def test_summarize_deal():
    summary = summarize_deal(make_deal("Steam", 7.49, 14.99, 50))
    assert summary["shop"] == "Steam"
    assert summary["discount_percent"] == 50
    assert summary["drm"] == ["Steam"]


# ---- Edge cases ----


def test_free_game_is_not_a_sale():
    assert best_deal([make_deal("Steam", 0.0, 0.0, 0)]) is None


def test_giveaway_at_full_discount_is_a_sale():
    deal = best_deal([make_deal("Epic", 0.0, 19.99, 100), make_deal("Steam", 19.99, 19.99, 0)])
    assert deal["shop"]["name"] == "Epic"


def test_same_price_everywhere_is_not_a_sale():
    assert best_deal([make_deal("Steam", 14.99, 14.99), make_deal("GOG", 14.99, 14.99)]) is None


def test_deals_without_any_regular_price_are_not_a_sale():
    deal = make_deal("Odd Shop", 5.0, 10.0, 50)
    del deal["regular"]
    assert best_deal([deal]) is None


def test_cheapest_deal_without_a_regular_price_is_compared_with_other_shops():
    odd = make_deal("Odd Shop", 5.0, 10.0, 50)
    del odd["regular"]
    assert best_deal([odd, make_deal("Steam", 10.0, 10.0)])["shop"]["name"] == "Odd Shop"
    assert summarize_deal(odd)["regular_price"] is None


def test_regional_regular_price_differences_use_the_lowest():
    # Steam's regular price is $30, but GOG's is $20, so $25 on Steam is not really a sale.
    assert best_deal([make_deal("Steam", 25.0, 30.0, 17), make_deal("GOG", 20.0, 20.0)]) is None


def test_threshold_boundaries():
    assert passes_thresholds(0.0, 100, 100, 0.0)
    assert passes_thresholds(4.99, 75, None, 4.99)
    assert not passes_thresholds(5.00, 75, None, 4.99)
    assert not passes_thresholds(4.99, 74, 75, None)


# ---- Discount against the market price ----


def test_market_discount_uses_the_lowest_regular_price():
    deals = [make_deal("Pricey", 30.0, 60.0, 50), make_deal("Fair", 35.0, 35.0, 0)]
    deal = best_deal(deals)
    assert deal["shop"]["name"] == "Pricey"
    assert market_msrp(deals) == 35.0
    assert market_discount_percent(30.0, 35.0) == 14
    assert summarize_deal(deal, market_msrp(deals))["market_discount_percent"] == 14


def test_market_discount_rounds_down():
    # 39.94% off must not pass a 40% minimum.
    assert market_discount_percent(60.06, 100.0) == 39
    assert market_discount_percent(30.0, 60.0) == 50
    assert market_discount_percent(0.0, 19.99) == 100


def test_market_discount_without_a_sale():
    assert market_discount_percent(20.0, 20.0) == 0
    assert market_discount_percent(25.0, 20.0) == 0
    assert market_discount_percent(5.0, None) == 0


# ---- Missing and null fields from ITAD ----


def test_null_or_missing_deal_lists():
    assert best_deal(None) is None
    assert market_msrp(None) is None
    assert usable_deals(None) == []


def test_deals_without_a_usable_price_are_ignored():
    broken = [{"shop": {"name": "A"}}, {"price": None}, {"price": {"amount": None}}, {"price": {"amount": "5"}}]
    good = make_deal("Steam", 5.0, 10.0, 50)
    assert usable_deals([*broken, good]) == [good]
    assert best_deal([*broken, good]) is good


def test_a_regular_price_of_zero_does_not_hide_real_sales():
    deals = [make_deal("Steam", 9.99, 19.99, 50), make_deal("Giveaway Shop", 0.0, 0.0, 0)]
    deals[1]["price"]["amount"] = 15.0
    assert market_msrp(deals) == 19.99
    assert best_deal(deals)["shop"]["name"] == "Steam"


def test_summary_tolerates_null_and_missing_fields():
    deal = {"price": {"amount": 5.0}, "regular": None, "shop": None, "drm": None}
    summary = summarize_deal(deal)
    assert summary == {
        "shop": None, "price": 5.0, "regular_price": None, "currency": None,
        "discount_percent": 0, "url": None, "expiry": None, "drm": [],
    }
