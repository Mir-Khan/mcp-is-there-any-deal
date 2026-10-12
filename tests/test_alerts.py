"""Tests for the scheduled alert job, with ITAD faked and Discord replaced by a recorder or a local server."""

import json
import logging
import socket
from datetime import datetime, timedelta, timezone

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import aiohttp

from deals_mcp import alerts, config, db
from deals_mcp.alerts import Alert, AlertError, DiscordNotifier, build_embed, check_webhook_url, parse_expiry, plan_alerts, run_alerts
from deals_mcp.itad import ITADClient

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
HK = "00000000-0000-4000-8000-000000000001"
CELESTE = "00000000-0000-4000-8000-000000000002"
HADES = "00000000-0000-4000-8000-000000000003"
WEBHOOK_SECRET = "webhook-token-abc"


def make_deal(shop, price, regular, cut, url=None, expiry=None):
    return {
        "shop": {"name": shop},
        "price": {"amount": price, "currency": "USD"},
        "regular": {"amount": regular, "currency": "USD"},
        "cut": cut,
        "url": url or f"https://example.com/{shop}/{price}",
        "expiry": expiry,
        "drm": [{"name": "Steam"}],
    }


class FakeITAD:
    """Prices per game id that each test can change between runs; None means the request fails."""

    def __init__(self):
        self.prices_by_id: dict[str, list] = {}
        self.fail = False

    async def prices(self, game_ids, country):
        if self.fail:
            from deals_mcp.itad import ITADError

            raise ITADError("IsThereAnyDeal did not respond in time; try again shortly.")
        return [{"id": gid, "deals": self.prices_by_id[gid]} for gid in game_ids if gid in self.prices_by_id]


class Recorder:
    """Stands in for Discord, recording each message's embeds; it can be told to fail once."""

    def __init__(self):
        self.messages: list[list[dict]] = []
        self.fail_on_message: int | None = None

    async def __call__(self, embeds):
        if self.fail_on_message == len(self.messages):
            self.fail_on_message = None
            raise AlertError("Discord returned HTTP 500: oops")
        self.messages.append(embeds)

    def titles(self):
        return [e["title"] for message in self.messages for e in message]


@pytest.fixture
async def setup(tmp_path):
    path = str(tmp_path / "wishlist.db")
    await db.init_db(path)
    fake = FakeITAD()
    return path, fake, Recorder()


async def run(path, fake, send, now=NOW):
    return await run_alerts(path, fake, send, now=now)


# ---- plan_alerts ----


def deal_summary(game_id, price, url="https://example.com/a", expiry=None):
    return {"game_id": game_id, "title": game_id, "price": price, "url": url, "expiry": expiry}


def state(url="https://example.com/a", price=10.0, ending_sent=False):
    return {"deal_url": url, "price": price, "ending_alert_sent": int(ending_sent)}


def test_plan_alerts_rules():
    soon = (NOW + timedelta(hours=3)).isoformat()
    later = (NOW + timedelta(days=3)).isoformat()
    deals = [
        deal_summary("never-sent", 10.0),
        deal_summary("same", 10.0),
        deal_summary("new-link", 10.0, url="https://example.com/b"),
        deal_summary("cheaper", 8.0),
        deal_summary("dearer", 12.0),
        deal_summary("ending", 10.0, expiry=soon),
        deal_summary("ending-reminded", 10.0, expiry=soon),
        deal_summary("ends-later", 10.0, expiry=later),
    ]
    states = {
        "same": state(),
        "new-link": state(),
        "cheaper": state(),
        "dearer": state(),
        "ending": state(),
        "ending-reminded": state(ending_sent=True),
        "ends-later": state(),
    }
    planned = [(a.kind, a.deal["game_id"]) for a in plan_alerts(deals, states, NOW)]
    assert planned == [("new", "never-sent"), ("new", "new-link"), ("new", "cheaper"), ("ending", "ending")]


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2026-10-12T18:00:00+02:00", datetime(2026, 10, 12, 16, 0, tzinfo=timezone.utc)),
        ("2026-10-12T18:00:00Z", datetime(2026, 10, 12, 18, 0, tzinfo=timezone.utc)),
        ("2026-10-12T18:00:00", datetime(2026, 10, 12, 18, 0, tzinfo=timezone.utc)),
        (1760292000, datetime.fromtimestamp(1760292000, timezone.utc)),
        (None, None),
        ("", None),
        ("not a date", None),
    ],
)
def test_parse_expiry(value, expected):
    assert parse_expiry(value) == expected


def test_embed_contents():
    deal = {
        "game_id": HK, "title": "Hollow Knight", "price": 7.49, "regular_price": 14.99, "currency": "USD",
        "shop": "Steam", "url": "https://example.com/hk", "expiry": "2026-10-12T18:00:00+00:00",
        "drm": ["Steam"], "discount_percent": 50, "market_discount_percent": 50, "country": "US",
    }
    embed = build_embed(Alert("new", deal))
    assert embed["title"] == "Hollow Knight: 50% off"
    assert embed["url"] == "https://example.com/hk"
    assert embed["description"].split("\n") == [
        "**$7.49** at Steam", "Regular price $14.99", f"Ends <t:{int(parse_expiry(deal['expiry']).timestamp())}:R>", "DRM: Steam",
    ]
    assert build_embed(Alert("ending", deal))["title"] == "Ending soon: Hollow Knight"
    assert len(build_embed(Alert("new", {**deal, "title": "x" * 500}))["title"]) == 256
    assert "12.00 BRL" in build_embed(Alert("new", {**deal, "price": 12.0, "currency": "BRL"}))["description"]


def test_check_webhook_url():
    assert check_webhook_url("https://discord.com/api/webhooks/1/abc")
    for bad in (None, "", "http://discord.com/api/webhooks/1/abc", "https://example.com/hook"):
        with pytest.raises(AlertError):
            check_webhook_url(bad)


# ---- run_alerts ----


async def test_each_deal_is_alerted_once(setup):
    path, fake, send = setup
    await db.add_item(path, HK, "Hollow Knight", "US")
    fake.prices_by_id[HK] = [make_deal("Steam", 7.49, 14.99, 50, url="https://example.com/hk")]

    summary = await run(path, fake, send)
    assert summary["alerts_sent"] == 1
    assert send.titles() == ["Hollow Knight: 50% off"]

    # The same deal on the next run sends nothing.
    assert (await run(path, fake, send))["alerts_sent"] == 0
    assert len(send.messages) == 1


async def test_a_price_drop_or_a_new_sale_is_alerted_again(setup):
    path, fake, send = setup
    await db.add_item(path, HK, "Hollow Knight", "US")
    fake.prices_by_id[HK] = [make_deal("Steam", 7.49, 14.99, 50, url="https://example.com/hk")]
    await run(path, fake, send)

    fake.prices_by_id[HK] = [make_deal("Steam", 5.99, 14.99, 60, url="https://example.com/hk")]
    await run(path, fake, send)
    fake.prices_by_id[HK] = [make_deal("GOG", 5.99, 14.99, 60, url="https://example.com/gog")]
    await run(path, fake, send)
    assert send.titles() == ["Hollow Knight: 50% off", "Hollow Knight: 60% off", "Hollow Knight: 60% off"]


async def test_ending_soon_is_reminded_once(setup):
    path, fake, send = setup
    await db.add_item(path, HK, "Hollow Knight", "US")
    expiry = (NOW + timedelta(days=2)).isoformat()
    fake.prices_by_id[HK] = [make_deal("Steam", 7.49, 14.99, 50, url="https://example.com/hk", expiry=expiry)]
    await run(path, fake, send)

    # Two days later the same deal ends within a few hours, so one reminder is sent, and only one.
    await run(path, fake, send, now=NOW + timedelta(days=2) - timedelta(hours=2))
    await run(path, fake, send, now=NOW + timedelta(days=2) - timedelta(hours=1))
    assert send.titles() == ["Hollow Knight: 50% off", "Ending soon: Hollow Knight"]


async def test_a_new_deal_that_already_ends_soon_is_not_reminded_again(setup):
    path, fake, send = setup
    await db.add_item(path, HK, "Hollow Knight", "US")
    expiry = (NOW + timedelta(hours=4)).isoformat()
    fake.prices_by_id[HK] = [make_deal("Steam", 7.49, 14.99, 50, expiry=expiry)]
    await run(path, fake, send)
    await run(path, fake, send, now=NOW + timedelta(hours=1))
    assert send.titles() == ["Hollow Knight: 50% off"]


async def test_the_next_sale_after_one_ends_is_alerted(setup):
    path, fake, send = setup
    await db.add_item(path, HK, "Hollow Knight", "US")
    sale = [make_deal("Steam", 7.49, 14.99, 50, url="https://example.com/hk")]
    fake.prices_by_id[HK] = sale
    await run(path, fake, send)

    fake.prices_by_id[HK] = [make_deal("Steam", 14.99, 14.99, 0)]
    await run(path, fake, send)
    assert await db.get_alert_states(path) == {}

    # The same sale coming back (even with the same link) is a new sale and is alerted.
    fake.prices_by_id[HK] = sale
    await run(path, fake, send)
    assert send.titles() == ["Hollow Knight: 50% off", "Hollow Knight: 50% off"]


async def test_a_failed_check_does_not_cause_a_repeat_alert(setup):
    path, fake, send = setup
    await db.add_item(path, HK, "Hollow Knight", "US")
    await db.add_item(path, CELESTE, "Celeste", "GB")
    fake.prices_by_id[HK] = [make_deal("Steam", 7.49, 14.99, 50)]
    fake.prices_by_id[CELESTE] = [make_deal("Steam", 4.99, 19.99, 75)]
    await run(path, fake, send)

    # ITAD stops knowing Celeste's id for one run, which says nothing about the sale, so its alert
    # state is kept and it is not alerted again when it comes back.
    del fake.prices_by_id[CELESTE]
    summary = await run(path, fake, send)
    assert [g["title"] for g in summary["not_checked"]] == ["Celeste"]
    fake.prices_by_id[CELESTE] = [make_deal("Steam", 4.99, 19.99, 75)]
    await run(path, fake, send)
    assert sorted(send.titles()) == ["Celeste: 75% off", "Hollow Knight: 50% off"]


async def test_removed_games_are_forgotten(setup):
    path, fake, send = setup
    await db.add_item(path, HK, "Hollow Knight", "US")
    fake.prices_by_id[HK] = [make_deal("Steam", 7.49, 14.99, 50)]
    await run(path, fake, send)
    await db.remove_item(path, HK)
    await run(path, fake, send)
    assert await db.get_alert_states(path) == {}


async def test_thresholds_apply_to_alerts(setup):
    path, fake, send = setup
    await db.add_item(path, HK, "Hollow Knight", "US", max_price=5.0)
    fake.prices_by_id[HK] = [make_deal("Steam", 7.49, 14.99, 50)]
    await run(path, fake, send)
    assert send.messages == []
    await db.set_thresholds(path, HK, max_price=0)
    await run(path, fake, send)
    assert send.titles() == ["Hollow Knight: 50% off"]


async def test_dry_run_sends_and_records_nothing(setup):
    path, fake, send = setup
    await db.add_item(path, HK, "Hollow Knight", "US")
    fake.prices_by_id[HK] = [make_deal("Steam", 7.49, 14.99, 50)]
    summary = await run(path, fake, None)
    assert [a["title"] for a in summary["alerts"]] == ["Hollow Knight"]
    assert await db.get_alert_states(path) == {}
    # A real run afterwards still sends the alert.
    assert (await run(path, fake, send))["alerts_sent"] == 1


async def test_many_deals_are_split_into_messages_of_ten(setup):
    path, fake, send = setup
    for n in range(23):
        game_id = f"00000000-0000-4000-8000-{n + 100:012d}"
        await db.add_item(path, game_id, f"Game {n:02d}", "US")
        fake.prices_by_id[game_id] = [make_deal("Steam", 5.0, 10.0, 50)]
    await run(path, fake, send)
    assert [len(m) for m in send.messages] == [10, 10, 3]


async def test_a_failed_send_keeps_what_was_sent_and_resends_the_rest(setup):
    path, fake, send = setup
    for n in range(12):
        game_id = f"00000000-0000-4000-8000-{n + 100:012d}"
        await db.add_item(path, game_id, f"Game {n:02d}", "US")
        fake.prices_by_id[game_id] = [make_deal("Steam", 5.0, 10.0, 50)]
    send.fail_on_message = 1
    with pytest.raises(AlertError):
        await run(path, fake, send)
    assert len(await db.get_alert_states(path)) == 10

    await run(path, fake, send)
    assert [len(m) for m in send.messages] == [10, 2]


async def test_a_wishlist_check_that_fails_entirely_raises(setup):
    path, fake, send = setup
    await db.add_item(path, HK, "Hollow Knight", "US")
    fake.fail = True
    with pytest.raises(Exception, match="did not respond"):
        await run(path, fake, send)
    assert send.messages == []


# ---- DiscordNotifier against a local server ----


@pytest.fixture
async def fake_discord():
    state = {"responses": [], "requests": []}

    async def webhook(request):
        state["requests"].append(await request.json())
        status, body = state["responses"].pop(0) if state["responses"] else (204, None)
        return web.Response(status=status, text=json.dumps(body) if body is not None else None, content_type="application/json")

    app = web.Application()
    app.router.add_post("/api/webhooks/1/{token}", webhook)
    server = TestServer(app)
    await server.start_server()
    state["url"] = str(server.make_url(f"/api/webhooks/1/{WEBHOOK_SECRET}"))
    yield state
    await server.close()


async def send_with(url, embeds):
    async with aiohttp.ClientSession() as session:
        await DiscordNotifier(session, url).send(embeds)


async def test_discord_payload(fake_discord):
    await send_with(fake_discord["url"], [{"title": "@everyone Free Game"}])
    payload = fake_discord["requests"][0]
    assert payload["embeds"] == [{"title": "@everyone Free Game"}]
    assert payload["allowed_mentions"] == {"parse": []}


async def test_discord_rate_limit_is_retried_once(fake_discord, monkeypatch):
    waits = []

    async def no_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(alerts.asyncio, "sleep", no_sleep)
    fake_discord["responses"] = [(429, {"retry_after": 1.5})]
    await send_with(fake_discord["url"], [{"title": "x"}])
    assert waits == [1.5]
    assert len(fake_discord["requests"]) == 2

    fake_discord["responses"] = [(429, {"retry_after": 99}), (429, {"retry_after": 1})]
    with pytest.raises(AlertError, match="HTTP 429"):
        await send_with(fake_discord["url"], [{"title": "x"}])
    assert waits[-1] == 30


async def test_discord_errors_do_not_reveal_the_webhook(fake_discord, caplog):
    fake_discord["responses"] = [(404, {"message": "Unknown Webhook"})]
    with caplog.at_level(logging.DEBUG), pytest.raises(AlertError, match="HTTP 404") as info:
        await send_with(fake_discord["url"], [{"title": "x"}])
    assert WEBHOOK_SECRET not in str(info.value)
    assert not [r for r in caplog.records if r.name != "aiohttp.access" and WEBHOOK_SECRET in r.getMessage()]


async def test_unreachable_discord():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with pytest.raises(AlertError, match="Could not reach Discord") as info:
        await send_with(f"http://127.0.0.1:{port}/api/webhooks/1/{WEBHOOK_SECRET}", [{"title": "x"}])
    assert WEBHOOK_SECRET not in str(info.value)
    assert info.value.__cause__ is None


# ---- The command ----


@pytest.fixture
def command_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "load_env", lambda: None)
    monkeypatch.setenv("DEALS_DB_PATH", str(tmp_path / "wishlist.db"))
    monkeypatch.setenv("ITAD_API_KEY", "test-key")
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    fake = FakeITAD()
    monkeypatch.setattr(ITADClient, "prices", lambda self, *a, **kw: fake.prices(*a, **kw))
    return tmp_path, fake


def test_command_without_a_webhook_fails_clearly(command_env, caplog):
    assert alerts.main([]) == 2
    assert "DISCORD_WEBHOOK_URL is not set" in caplog.text


def test_command_dry_run(command_env, capsys):
    tmp_path, fake = command_env
    import anyio

    anyio.run(db.init_db, str(tmp_path / "wishlist.db"))
    anyio.run(db.add_item, str(tmp_path / "wishlist.db"), HK, "Hollow Knight", "US")
    fake.prices_by_id[HK] = [make_deal("Steam", 7.49, 14.99, 50)]
    assert alerts.main(["--dry-run"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert [a["embed"]["title"] for a in printed] == ["Hollow Knight: 50% off"]


def test_command_reports_a_failed_check(command_env, caplog):
    tmp_path, fake = command_env
    import anyio

    anyio.run(db.init_db, str(tmp_path / "wishlist.db"))
    anyio.run(db.add_item, str(tmp_path / "wishlist.db"), HK, "Hollow Knight", "US")
    fake.fail = True
    assert alerts.main(["--dry-run"]) == 1
    assert "did not respond in time" in caplog.text
