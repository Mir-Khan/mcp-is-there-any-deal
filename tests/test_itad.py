"""Tests for the ITAD client against a local HTTP server that imitates the API and its failures."""

import asyncio
import logging
import socket

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from deals_mcp.itad import ITADClient, ITADError

pytestmark = pytest.mark.anyio

SECRET = "secret-api-key-123"


@pytest.fixture
async def fake_api():
    """A local server; tests set `handler` to choose how it responds, and `requests` records calls."""
    state = {"handler": None, "requests": []}

    async def dispatch(request):
        state["requests"].append({"path": request.path, "query": dict(request.query), "body": await request.text()})
        return await state["handler"](request)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", dispatch)
    server = TestServer(app)
    await server.start_server()
    state["url"] = str(server.make_url("")).rstrip("/")
    yield state
    await server.close()


async def make_client(fake_api, timeout=None):
    session = aiohttp.ClientSession(timeout=timeout or aiohttp.ClientTimeout(total=5))
    return session, ITADClient(session, SECRET, base_url=fake_api["url"])


async def expect_error(fake_api, call, match, caplog, timeout=None):
    session, client = await make_client(fake_api, timeout)
    try:
        with caplog.at_level(logging.DEBUG), pytest.raises(ITADError, match=match) as info:
            await call(client)
    finally:
        await session.close()
    # The key is part of the request URL, so it must not leak through messages or logs.
    assert SECRET not in str(info.value)
    assert info.value.__cause__ is None
    # The fake server's own access log (aiohttp.access) records the URL it received, which stands in
    # for ITAD's side, so it is the one logger left out of the check.
    assert not [r for r in caplog.records if r.name != "aiohttp.access" and SECRET in r.getMessage()]
    return info.value


async def test_prices_always_include_shops_that_are_not_on_sale(fake_api):
    async def ok(request):
        return web.json_response([])

    fake_api["handler"] = ok
    session, client = await make_client(fake_api)
    async with session:
        await client.prices(["id-1"], "US")
    assert fake_api["requests"][0]["query"]["nondeals"] == "1"
    assert fake_api["requests"][0]["query"]["key"] == SECRET


async def test_search_requests_fifty_results(fake_api):
    async def ok(request):
        return web.json_response([{"id": "1", "title": "Doom"}])

    fake_api["handler"] = ok
    session, client = await make_client(fake_api)
    async with session:
        assert await client.search("Doom") == [{"id": "1", "title": "Doom"}]
    assert fake_api["requests"][0]["query"]["results"] == "50"


async def test_html_page_with_status_200(fake_api, caplog):
    async def html(request):
        return web.Response(text="<html>Down for maintenance</html>", content_type="text/html")

    fake_api["handler"] = html
    await expect_error(fake_api, lambda c: c.search("Doom"), "unexpected response", caplog)


async def test_json_with_the_wrong_content_type_is_still_parsed(fake_api):
    async def mislabelled(request):
        return web.Response(text='[{"id": "1", "title": "Doom"}]', content_type="text/plain")

    fake_api["handler"] = mislabelled
    session, client = await make_client(fake_api)
    async with session:
        assert await client.search("Doom") == [{"id": "1", "title": "Doom"}]


async def test_timeout(fake_api, caplog):
    async def slow(request):
        await asyncio.sleep(2)
        return web.json_response([])

    fake_api["handler"] = slow
    await expect_error(fake_api, lambda c: c.search("Doom"), "did not respond in time", caplog, aiohttp.ClientTimeout(total=0.2))


async def test_connection_refused(caplog):
    # Find a free port and leave it closed, so the connection is refused.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    await expect_error({"url": f"http://127.0.0.1:{port}"}, lambda c: c.search("Doom"), "Could not reach", caplog)


@pytest.mark.parametrize(
    "status, body, match",
    [
        (403, '{"reason_phrase":"Invalid or expired api key"}', "rejected ITAD_API_KEY"),
        (400, '{"reason_phrase":"Request body must be non-empty JSON array of game IDs"}', "not a valid IsThereAnyDeal game id"),
        (400, "{\"reason_phrase\":\"Unknown Country 'ZZ'\"}", "country code 'ZZ' is not supported"),
        (500, "Internal Server Error", "HTTP 500"),
        (429, "Too Many Requests", "limiting how often"),
        # An error page from a proxy that echoes the request URL, key included, must not be logged as is.
        (502, f"<html>Bad gateway for /games/prices/v2?key={SECRET}</html>", "HTTP 502"),
    ],
)
async def test_error_statuses(fake_api, caplog, status, body, match):
    async def error(request):
        return web.Response(status=status, text=body)

    fake_api["handler"] = error
    await expect_error(fake_api, lambda c: c.prices(["Hollow Knight"], "ZZ"), match, caplog)


async def test_missing_api_key_makes_no_request(fake_api):
    client = ITADClient(session=None, api_key=None, base_url=fake_api["url"])
    with pytest.raises(ITADError, match="ITAD_API_KEY is not set"):
        await client.search("Doom")
    assert fake_api["requests"] == []


async def test_invalid_url_is_reported_as_configuration(caplog):
    await expect_error({"url": "http://[not a host"}, lambda c: c.search("Doom"), "configuration problem", caplog)
