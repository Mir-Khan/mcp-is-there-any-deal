# mcp-is-there-any-deal

[![Tests](https://github.com/Mir-Khan/mcp-is-there-any-deal/actions/workflows/tests.yml/badge.svg)](https://github.com/Mir-Khan/mcp-is-there-any-deal/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)

An [MCP](https://modelcontextprotocol.io) server that lets Claude, or any other MCP client, look up PC game prices on [IsThereAnyDeal](https://isthereanydeal.com) and keep a wishlist of games with a price target for each one. It can answer questions such as "is anything on my wishlist on sale?", "what is the lowest Hollow Knight has ever been?" or "should I buy Celeste now or wait?", and an optional scheduled job posts new deals to a Discord channel.

```text
You:     Add Resident Evil 4 to my wishlist, but only tell me about it under $15.
Claude:  Several games match "Resident Evil 4". Which one did you mean?
           ( ) Resident Evil 4 (2005)
           ( ) Resident Evil 4 (2023)
           (•) Resident Evil 4 (2023) - Gold Edition
           ( ) Resident Evil 4 Ultimate HD Edition
Claude:  Added Resident Evil 4 (2023) - Gold Edition with a $15 price cap. It is $13.75 at
         GamesPlanet right now, 72% off, so it already meets your target.
```

## Highlights

- **Built on the MCP Python SDK version 2**, using all three MCP primitives (tools, resources and prompts), along with elicitation, which lets a tool stop and ask the user a question partway through a call.
- **Search that resolves franchise names.** IsThereAnyDeal's own search is fuzzy, so for "Final Fantasy" it lists XV and the VII remake first and the original game 24th. The server re-ranks the results by comparing normalized titles, so case, punctuation, accents, trademark symbols and Roman numerals do not matter ("final fantasy 7" matches "FINAL FANTASY VII"), and a single exact match is used directly.
- **It asks rather than guesses.** When a name is ambiguous, such as "Resident Evil 4" (2005 and 2023) or "Halo", the user picks from a list in clients that support elicitation, and the model asks in the conversation in clients that do not. A query with no close match is reported as such, instead of returning unrelated games.
- **A deal rule that ignores fake sales.** A price only counts as a sale when it is below the lowest regular price at any shop, so "50% off" from an inflated regular price is ignored, and the minimum discount the user sets is measured the same way.
- **Discord alerts with no repeats.** The alert job remembers what it has sent, so each deal is posted once, with one reminder before it ends, and a failed request never causes a repeat alert.
- **Tested in depth.** The test suite of more than 170 tests runs without a network connection. It covers the deal rule, title matching against saved real search results, the database, the API client against a local fake server (including timeouts, error pages and rate limits, and checking that the API key never leaks into errors or logs), and every tool, resource and prompt through an in-process MCP client.

## Requirements

- Python 3.11 or newer.
- A free IsThereAnyDeal API key, from https://isthereanydeal.com/apps/my/.
- An MCP client, such as [Claude Code](https://claude.com/claude-code) or Claude Desktop.

The server runs on Windows, macOS and Linux, and the tests run on all three in CI.

## Installation

**macOS and Linux:**

```bash
git clone https://github.com/Mir-Khan/mcp-is-there-any-deal.git
cd mcp-is-there-any-deal
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
cp .env.example .env    # then add your ITAD_API_KEY
```

**Windows (PowerShell):**

```powershell
git clone https://github.com/Mir-Khan/mcp-is-there-any-deal.git
cd mcp-is-there-any-deal
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
copy .env.example .env    # then add your ITAD_API_KEY
```

### Configuration

| Setting | Description |
|---|---|
| `ITAD_API_KEY` | Your IsThereAnyDeal API key (required). |
| `DEFAULT_COUNTRY` | The two-letter country used for prices when none is given, such as `US` or `GB` (default `US`). |
| `DEALS_DB_PATH` | Where the wishlist is stored (default `~/.deals-mcp/wishlist.db`). |
| `DISCORD_WEBHOOK_URL` | Only needed for deal alerts. |

Settings are read from the environment and then from the first `.env` file that sets them, out of the file named by `DEALS_ENV_FILE`, the project folder's `.env`, and `~/.deals-mcp/.env`. The current directory is not used, since the client may start the server from anywhere.

## Connecting a client

The server uses the stdio transport, so the client starts it as a local process. In the commands below, replace `/path/to/mcp-is-there-any-deal` with the folder you cloned into, and on Windows use `.venv\Scripts\python.exe` in place of `.venv/bin/python`.

**Claude Code:**

```bash
claude mcp add deals --scope user -- /path/to/mcp-is-there-any-deal/.venv/bin/python -m deals_mcp.server
```

Then run `/mcp` inside Claude Code in order to check that it is connected.

**Claude Desktop:** open Settings, then Developer, then Edit Config (the file is `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS and `%APPDATA%\Claude\claude_desktop_config.json` on Windows), add the following, and restart the app:

```json
{
  "mcpServers": {
    "deals": {
      "command": "/path/to/mcp-is-there-any-deal/.venv/bin/python",
      "args": ["-m", "deals_mcp.server"]
    }
  }
}
```

On Windows, write the path with doubled backslashes, for example `"C:\\Users\\you\\mcp-is-there-any-deal\\.venv\\Scripts\\python.exe"`.

**Other clients:** any client that supports local (stdio) MCP servers, such as VS Code or Cursor, can use the same command and arguments.

Since the server runs on your computer, it cannot be added to the claude.ai website or the mobile apps, which only connect to MCP servers hosted on the internet.

## What it exposes

**Tools**

| Tool | Description |
|---|---|
| `search_game(query, limit, ask_if_ambiguous)` | Searches by title and returns ranked results with a match level, asking the user to choose when the name is ambiguous. |
| `get_current_deals(game_id, country)` | Returns the current price at every shop, along with whether the cheapest offer is a real sale. |
| `get_historical_low(game_id, country)` | Returns the all-time low price and the current best price. |
| `add_to_wishlist(game_id, title, country, min_discount_percent, max_price)` | Adds a game, or replaces its entry, with an optional minimum discount and price cap. |
| `remove_from_wishlist(game_id_or_title)` | Removes a game, matching by id or by title. |
| `list_wishlist()` | Lists the wishlist. |
| `set_thresholds(game_id_or_title, min_discount_percent, max_price)` | Changes a game's minimum discount or price cap; a `max_price` of 0 removes the cap. |
| `check_wishlist_deals()` | Checks the whole wishlist, and returns the games that are on sale and meet their thresholds, along with any games that could not be checked and the reason. |

**Resources:** `wishlist://items` (the whole wishlist as JSON) and `wishlist://items/{game_id}` (a single entry).

**Prompts:** `weekly_deal_summary` (rank the deals on the wishlist) and `should_i_buy(title)` (compare the current price with the all-time low and recommend buying now or waiting).

Game ids are IsThereAnyDeal ids, which `search_game` returns; anything else, such as a title passed by mistake, is rejected.

## Deal alerts (optional)

An MCP server only runs while a client has it open and cannot message anyone on its own, so alerts come from a separate command, `python -m deals_mcp.alerts`, which is meant to run on a schedule. It uses the same wishlist and the same deal check as the `check_wishlist_deals` tool, and posts each new deal to a Discord channel.

Each deal is posted once. A game is posted again when its price drops, when a different sale or shop becomes the best deal, or when a sale that had ended comes back, and a deal that ends within 8 hours gets one "ending soon" reminder.

1. In Discord, open the channel's settings, then Integrations, Webhooks, New Webhook, and copy the URL.
2. Add it to `.env` as `DISCORD_WEBHOOK_URL=...`. Anyone with this URL can post to the channel, so keep it private.
3. Send a test message with `.venv/bin/python -m deals_mcp.alerts --test`, and see what would be posted, without posting it, with `--dry-run`.
4. Schedule it:
   - **Windows:** run `.\scripts\register-alerts-task.ps1`, which creates a Task Scheduler task that runs every 6 hours (`-Hours 3` changes this, and `-Remove` deletes the task). It uses `pythonw.exe`, so no window opens.
   - **macOS and Linux:** add a cron entry with `crontab -e`, for example to run every 6 hours:

     ```text
     0 */6 * * * /path/to/mcp-is-there-any-deal/.venv/bin/python -m deals_mcp.alerts
     ```

     On macOS, cron may need Full Disk Access in System Settings, under Privacy and Security, if the project is in a protected folder such as Documents; a `launchd` agent is the alternative.

Each run is logged to `~/.deals-mcp/alerts.log`. The command exits with 0 on success, 1 when the wishlist check fails, and 2 when Discord cannot be reached or the webhook is not set.

## Development

```bash
.venv/bin/python -m pytest                                                     # run the tests
npx @modelcontextprotocol/inspector .venv/bin/python -m deals_mcp.server        # call tools by hand
```

The [MCP Inspector](https://github.com/modelcontextprotocol/inspector) is useful for calling tools, reading resources and rendering prompts without a model. The SDK's own `mcp dev` command is not used, because it always starts the server through `uv`.

### Project layout

- `src/deals_mcp/server.py`: the MCP server, with every tool, resource and prompt.
- `src/deals_mcp/matching.py`: title normalization and the ranking of search results.
- `src/deals_mcp/itad.py`: the IsThereAnyDeal API client.
- `src/deals_mcp/deal_logic.py`: pure functions that decide whether a price is a real sale.
- `src/deals_mcp/deal_check.py`: the wishlist check, shared by the MCP tool and the alert job.
- `src/deals_mcp/db.py`: the SQLite wishlist, and the record of which alerts were sent.
- `src/deals_mcp/alerts.py`: the scheduled alert job and the Discord webhook sender.
- `src/deals_mcp/config.py`: settings from the environment and `.env` files.
- `scripts/register-alerts-task.ps1`: registers the alert job with Windows Task Scheduler.
- `tests/fixtures/itad_search.json`: real IsThereAnyDeal search results for several franchise queries, so that the ranking tests run against actual titles without a network connection.

The server logs to stderr, because stdout carries the stdio transport's messages, so a stray `print()` in the server would break the connection.

## License

[MIT](LICENSE)
