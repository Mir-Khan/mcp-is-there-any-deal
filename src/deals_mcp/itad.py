"""A small async client for the IsThereAnyDeal (ITAD) API v2, ported from discord-deal-bot."""

import asyncio
import logging

import aiohttp
from mcp.server.mcpserver.exceptions import ToolError

logger = logging.getLogger(__name__)

BASE_URL = "https://api.isthereanydeal.com"

# Without this, aiohttp waits up to five minutes, which would leave a tool call hanging.
DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=20)


class ITADError(ToolError):
    """Raised when the ITAD API returns an error that the caller should see.

    It subclasses ToolError because MCPServer only passes the message of a ToolError on to the
    model; any other exception is reported as a generic "Error executing tool".
    """


class ITADClient:
    def __init__(self, session: aiohttp.ClientSession, api_key: str | None, base_url: str = BASE_URL):
        self.session = session
        self.api_key = api_key
        self.base_url = base_url

    async def _request(self, endpoint, params=None, method="GET", json_data=None):
        if not self.api_key:
            raise ITADError("ITAD_API_KEY is not set, so the IsThereAnyDeal API cannot be used.")
        params = dict(params or {})
        params["key"] = self.api_key

        # Network and parsing errors are turned into ITADError with a plain message, raised "from
        # None", because aiohttp's own exceptions include the request URL, and so the API key.
        try:
            async with self.session.request(method, f"{self.base_url}/{endpoint}", params=params, json=json_data) as resp:
                if resp.status == 200:
                    # content_type=None parses the body whatever its declared type, so an HTML
                    # page (from a CDN or a maintenance screen) fails below as invalid JSON.
                    return await resp.json(content_type=None)
                error_text = await resp.text()
        # The order matters: InvalidURL is both a ClientError and a ValueError, and a timeout can be
        # a ClientError too, so the specific cases come before the general ones.
        except aiohttp.InvalidURL:
            logger.error("ITAD API URL is invalid for %s", endpoint)
            raise ITADError("The IsThereAnyDeal API address is invalid, which is a configuration problem.") from None
        except asyncio.TimeoutError:
            logger.error("ITAD API timed out on %s", endpoint)
            raise ITADError("IsThereAnyDeal did not respond in time; try again shortly.") from None
        except aiohttp.ClientError as e:
            logger.error("ITAD API request failed on %s: %s", endpoint, type(e).__name__)
            raise ITADError("Could not reach IsThereAnyDeal; check the network connection.") from None
        except ValueError:
            logger.error("ITAD API returned a response that is not JSON on %s", endpoint)
            raise ITADError("IsThereAnyDeal returned an unexpected response; try again shortly.") from None

        # An error page from a proxy can echo the request URL, so the key is masked and the body cut short.
        logged_body = error_text.replace(self.api_key, "<redacted>")[:500]
        logger.error("ITAD API error %s on %s: %s", resp.status, endpoint, logged_body)
        lowered = error_text.lower()
        if resp.status in (401, 403):
            raise ITADError("IsThereAnyDeal rejected ITAD_API_KEY as invalid or expired.")
        if resp.status == 429:
            raise ITADError("IsThereAnyDeal is limiting how often it can be called; try again in a minute.")
        if resp.status == 400 and "game id" in lowered:
            raise ITADError("That is not a valid IsThereAnyDeal game id; use search_game to find the id.")
        # Unsupported country codes usually come back as a 400 Bad Request.
        if resp.status == 400 and ("country" in lowered or "region" in lowered):
            raise ITADError(f"The country code '{params.get('country')}' is not supported by IsThereAnyDeal.")
        raise ITADError(f"IsThereAnyDeal returned HTTP {resp.status} for {endpoint}.")

    async def search(self, title: str, results: int = 50) -> list[dict]:
        """Search games by title, returning a list of {id, slug, title, type, ...}.

        ITAD only returns 20 results by default, which is not always enough: for "Final Fantasy" the
        original game is result 24, so more are requested and then re-ranked by the caller.
        """
        return await self._request("games/search/v1", {"title": title, "results": results}) or []

    async def prices(self, game_ids: list[str], country: str) -> list[dict]:
        """Current prices at every shop for several games in one call, as [{id, deals: [...]}, ...].

        Shops that are not discounted are always included (nondeals=1), because deal_logic compares
        the cheapest price against the lowest regular price across all shops, and leaving them out
        would make a small "sale" look real when another shop sells it cheaper at full price.
        Games that ITAD does not know are missing from the response.
        """
        if not game_ids:
            return []
        params = {"country": country, "nondeals": 1, "vouchers": 1}
        return await self._request("games/prices/v2", params, method="POST", json_data=game_ids) or []

    async def overview(self, game_ids: list[str], country: str) -> dict:
        """Overview data, which includes the historical low, as {prices: [...], bundles: [...]}."""
        return await self._request("games/overview/v2", {"country": country}, method="POST", json_data=game_ids) or {}
