"""Sends Discord alerts for wishlist deals; meant to run on a schedule, separately from the MCP server.

An MCP server only runs while a client has it open and cannot message anyone by itself, so alerts
come from this job instead, which Windows Task Scheduler (or cron) runs every few hours. It shares
the wishlist and the deal check with the MCP server, and it remembers which deals it has already
sent, so that each deal is alerted once, with at most one "ending soon" reminder.

Usage:
    python -m deals_mcp.alerts             check the wishlist and send alerts for new deals
    python -m deals_mcp.alerts --dry-run   show the alerts that would be sent, without sending them
    python -m deals_mcp.alerts --test      send a test message, in order to check the webhook
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from typing import Any

import aiohttp

from deals_mcp import config, db, deal_check
from deals_mcp.itad import DEFAULT_TIMEOUT, ITADClient

logger = logging.getLogger("deals_mcp.alerts")

# A deal that ends within this time gets one "ending soon" reminder, as in discord-deal-bot.
ENDING_SOON = timedelta(hours=8)
# Discord accepts at most 10 embeds in one webhook message.
MAX_EMBEDS = 10
WEBHOOK_PREFIXES = (
    "https://discord.com/api/webhooks/",
    "https://discordapp.com/api/webhooks/",
    "https://ptb.discord.com/api/webhooks/",
    "https://canary.discord.com/api/webhooks/",
)
CURRENCY_SYMBOLS = {"USD": "$", "GBP": "£", "EUR": "€", "CAD": "C$", "AUD": "A$", "JPY": "¥"}
NEW_DEAL_COLOR = 0x2ECC71
ENDING_COLOR = 0xE67E22

Sender = Callable[[list[dict]], Awaitable[None]]


class AlertError(Exception):
    """Raised when an alert cannot be sent."""


@dataclass
class Alert:
    kind: str  # "new" for a new or cheaper deal, "ending" for a reminder that a deal ends soon
    deal: dict


# ---- Deciding what to send ----


def parse_expiry(expiry: Any) -> datetime | None:
    """ITAD gives expiry as an ISO date string (or null); a Unix timestamp is accepted too."""
    if expiry is None or expiry == "":
        return None
    try:
        if isinstance(expiry, (int, float)):
            return datetime.fromtimestamp(expiry, timezone.utc)
        parsed = datetime.fromisoformat(str(expiry))
    except (ValueError, OverflowError, OSError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def ends_soon(deal: dict, now: datetime) -> bool:
    expiry = parse_expiry(deal.get("expiry"))
    return expiry is not None and now < expiry <= now + ENDING_SOON


def plan_alerts(deals: list[dict], states: dict[str, dict], now: datetime) -> list[Alert]:
    """Pick the deals worth an alert, given what was already sent for each game.

    A deal is new when nothing was sent for the game yet, when the deal link has changed (a new sale
    or another shop), or when the price has dropped since the last alert. An unchanged deal only gets
    a reminder once, when it is about to end.
    """
    alerts = []
    for deal in deals:
        state = states.get(deal["game_id"])
        if state is None or state["deal_url"] != deal.get("url") or deal["price"] < state["price"]:
            alerts.append(Alert("new", deal))
        elif not state["ending_alert_sent"] and ends_soon(deal, now):
            alerts.append(Alert("ending", deal))
    return alerts


# ---- Formatting ----


def format_price(amount: float | None, currency: str | None) -> str:
    if amount is None:
        return "?"
    symbol = CURRENCY_SYMBOLS.get(currency or "")
    return f"{symbol}{amount:.2f}" if symbol else f"{amount:.2f} {currency or ''}".strip()


def build_embed(alert: Alert) -> dict:
    deal = alert.deal
    currency = deal.get("currency")
    if alert.kind == "new":
        title = f"{deal['title']}: {deal.get('market_discount_percent', deal.get('discount_percent', 0))}% off"
    else:
        title = f"Ending soon: {deal['title']}"

    lines = [f"**{format_price(deal['price'], currency)}** at {deal.get('shop') or 'an unknown shop'}"]
    if deal.get("regular_price"):
        lines.append(f"Regular price {format_price(deal['regular_price'], currency)}")
    expiry = parse_expiry(deal.get("expiry"))
    if expiry:
        # Discord shows this as a relative time, such as "in 5 hours", in the reader's own time zone.
        lines.append(f"Ends <t:{int(expiry.timestamp())}:R>")
    if deal.get("drm"):
        lines.append("DRM: " + ", ".join(d for d in deal["drm"] if d))

    embed = {
        "title": title[:256],
        "description": "\n".join(lines)[:4096],
        "color": NEW_DEAL_COLOR if alert.kind == "new" else ENDING_COLOR,
        "footer": {"text": f"IsThereAnyDeal, {deal.get('country', '')}".strip(", ")},
    }
    if deal.get("url"):
        embed["url"] = deal["url"]
    return embed


# ---- Sending ----


def check_webhook_url(url: str | None) -> str:
    if not url:
        raise AlertError("DISCORD_WEBHOOK_URL is not set; add it to the .env file.")
    if not url.startswith(WEBHOOK_PREFIXES):
        raise AlertError("DISCORD_WEBHOOK_URL does not look like a Discord webhook URL.")
    return url


class DiscordNotifier:
    """Posts embeds to a Discord webhook. The URL contains a secret token, so it is never logged."""

    def __init__(self, session: aiohttp.ClientSession, webhook_url: str):
        self.session = session
        self.webhook_url = webhook_url

    async def send(self, embeds: list[dict]) -> None:
        # allowed_mentions stops a game title such as "@everyone" from pinging anyone.
        payload = {"username": "IsThereAnyDeal", "embeds": embeds, "allowed_mentions": {"parse": []}}
        for attempt in range(2):
            try:
                async with self.session.post(self.webhook_url, json=payload) as resp:
                    if resp.status in (200, 204):
                        return
                    if resp.status == 429 and attempt == 0:
                        # Discord says how long to wait; one retry is enough for a job that runs every few hours.
                        try:
                            retry_after = float((await resp.json(content_type=None)).get("retry_after", 1))
                        except (ValueError, AttributeError, TypeError):
                            retry_after = 1.0
                        await asyncio.sleep(min(retry_after, 30))
                        continue
                    body = (await resp.text())[:300]
            except asyncio.TimeoutError:
                raise AlertError("Discord did not respond in time.") from None
            except aiohttp.ClientError as e:
                raise AlertError(f"Could not reach Discord ({type(e).__name__}).") from None
            raise AlertError(f"Discord returned HTTP {resp.status}: {body}")
        raise AlertError("Discord is still rate limiting the webhook.")


# ---- The job ----


async def run_alerts(db_path: str, itad: ITADClient, send: Sender | None, now: datetime | None = None) -> dict:
    """Check the wishlist, send alerts for new deals, and record them.

    With send=None (a dry run) nothing is sent or recorded, and the planned alerts are returned. If
    sending fails part of the way through, the messages already sent stay recorded, so a later run
    only resends the rest.
    """
    now = now or datetime.now(timezone.utc)
    await db.init_db(db_path)
    items = await db.list_items(db_path)
    report = await deal_check.check_wishlist(itad, items)
    alerts = plan_alerts(report["deals"], await db.get_alert_states(db_path), now)
    summary = {
        "games_checked": report["games_checked"],
        "games_on_sale": report["games_on_sale"],
        "alerts": [{"kind": a.kind, "title": a.deal["title"], "embed": build_embed(a)} for a in alerts],
        "alerts_sent": 0,
        "not_checked": report.get("not_checked", []),
    }
    if send is None:
        return summary

    for i in range(0, len(alerts), MAX_EMBEDS):
        chunk = alerts[i : i + MAX_EMBEDS]
        await send([build_embed(a) for a in chunk])
        for alert in chunk:
            deal = alert.deal
            # A new deal that already ends soon is not reminded about again a few hours later.
            ending_sent = alert.kind == "ending" or ends_soon(deal, now)
            await db.record_alert(db_path, deal["game_id"], deal.get("url"), deal["price"], ending_sent)
        summary["alerts_sent"] += len(chunk)

    # A game that was checked and is no longer on sale (or no longer meets its thresholds) is
    # forgotten, so that its next sale is alerted; games that could not be checked are left alone,
    # since a failed request says nothing about whether the sale is still on.
    on_sale = {d["game_id"] for d in report["deals"]}
    unchecked = {g["game_id"] for g in summary["not_checked"]}
    await db.forget_alerts(db_path, [i["game_id"] for i in items if i["game_id"] not in on_sale | unchecked])
    await db.forget_alerts_for_removed_games(db_path)
    return summary


def setup_logging() -> None:
    """Log to ~/.deals-mcp/alerts.log, since a scheduled run has no console, and to stderr if there is one."""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(config.DATA_DIR / "alerts.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    ]
    # pythonw.exe, which Task Scheduler uses so that no window opens, has no stderr at all.
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s:%(levelname)s:%(name)s: %(message)s", handlers=handlers)


async def _run(args: argparse.Namespace) -> int:
    webhook_url = None if args.dry_run else check_webhook_url(os.getenv("DISCORD_WEBHOOK_URL"))
    async with aiohttp.ClientSession(timeout=DEFAULT_TIMEOUT) as session:
        send = DiscordNotifier(session, webhook_url).send if webhook_url else None
        if args.test:
            await send([{"title": "Test alert", "description": "The deal alerts webhook works.", "color": NEW_DEAL_COLOR}])
            logger.info("Sent a test message.")
            return 0

        itad = ITADClient(session, os.getenv("ITAD_API_KEY"))
        summary = await run_alerts(config.get_db_path(), itad, send)

    logger.info(
        "Checked %s games, %s on sale, %s alerts sent.",
        summary["games_checked"], summary["games_on_sale"], summary["alerts_sent"],
    )
    for game in summary["not_checked"]:
        logger.warning("Could not check %s (%s): %s", game["title"], game["country"], game["reason"])
    if args.dry_run and sys.stdout is not None:
        print(json.dumps(summary["alerts"], indent=2, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mcp-is-there-any-deal-alerts", description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="show the alerts that would be sent, without sending them")
    mode.add_argument("--test", action="store_true", help="send a test message to the Discord webhook")
    args = parser.parse_args(argv)

    config.load_env()
    setup_logging()
    try:
        return asyncio.run(_run(args))
    except AlertError as e:
        logger.error("%s", e)
        return 2
    except Exception as e:
        # ITADError and the other expected errors carry a readable message without the URL or key.
        logger.error("The alert run failed: %s", e if str(e) else type(e).__name__)
        return 1


if __name__ == "__main__":
    sys.exit(main())
