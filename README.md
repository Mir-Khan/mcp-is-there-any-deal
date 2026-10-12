# mcp-is-there-any-deal

An MCP server that lets Claude look up PC game prices on [IsThereAnyDeal](https://isthereanydeal.com) and manage a local wishlist, in order to answer questions such as "is anything on my wishlist on sale?" or "should I buy Celeste now or wait?". The price code and the deal rule are ported from `discord-deal-bot`, although this server keeps its own SQLite wishlist and does not touch the bot's database.

It is built on version 2 of the official Python MCP SDK, where the class that used to be called `FastMCP` is now `MCPServer`.

## What it exposes

**Tools**

| Tool | Description |
|---|---|
| `search_game(query, limit)` | Searches ITAD by title and returns game ids, which the other tools need. |
| `get_current_deals(game_id, country)` | Returns the current price at every shop, along with whether the cheapest offer is a real sale. |
| `get_historical_low(game_id, country)` | Returns the all-time low price and the current best price. |
| `add_to_wishlist(game_id, title, country, min_discount_percent, max_price)` | Adds or replaces a wishlist entry. |
| `remove_from_wishlist(game_id_or_title)` | Removes an entry, matching by id or by title. |
| `list_wishlist()` | Lists the wishlist. |
| `set_thresholds(game_id_or_title, min_discount_percent, max_price)` | Changes the thresholds of an entry; a `max_price` of 0 removes the cap. |
| `check_wishlist_deals()` | Checks the whole wishlist and returns the games that are on sale and that meet their thresholds. |

A deal only counts when its price is below the lowest regular price across all shops, which is the same "market MSRP" rule that the bot uses, so that a "sale" which still costs more than the normal price somewhere else is ignored. The minimum discount threshold is also measured against that lowest regular price (reported as `market_discount_percent`), rather than against the shop's own discount, which can be inflated by a high regular price. Games that cannot be checked, for example because ITAD no longer knows their id or a request failed, are listed under `not_checked` with the reason and are not counted in `games_checked`.

Game ids must be ITAD ids (UUIDs, as returned by `search_game`); they are trimmed and lowercased, and anything else, such as a title, is rejected.

### Searching and clarification

IsThereAnyDeal's own search order is fuzzy, so for "Final Fantasy" it lists XV and the VII remake first and the original game 24th. `search_game` therefore requests 50 results and re-ranks them with `matching.py`, which compares titles after normalizing case, punctuation, accents, trademark symbols, and Roman numerals against numbers (so "final fantasy 7" matches "FINAL FANTASY VII"). Each result carries a match level: `exact`, `variant`, `starts_with`, `contains`, or `weak`.

- A single exact match is used without asking, for example "Final Fantasy" resolves to FINAL FANTASY and not a sequel.
- When the search is ambiguous, such as "Resident Evil 4" (2005 and 2023) or "Halo", the server asks the user to pick a game through MCP elicitation, which shows a form in clients that support it. In clients that do not, or when the user dismisses the form, the result has `needs_clarification: true`, and the model is told to ask in the conversation.
- When nothing matches closely, such as a misspelling or a nonsense query, the user is not offered a list of unrelated games, and the result says so and suggests another spelling.

The question is asked through the SDK's `Resolve` and `Elicit` mechanism, which works with both the older protocol (a direct request partway through the call) and the 2026-07-28 protocol (the client answers and retries the call).

**Resources:** `wishlist://items` (the whole wishlist as JSON) and `wishlist://items/{game_id}` (a single entry).

**Prompts:** `weekly_deal_summary` and `should_i_buy(title)`.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
copy .env.example .env   # then fill in ITAD_API_KEY
```

An API key can be created at https://isthereanydeal.com/apps/my/. Settings are read from the first of these files that sets them: the file named by `DEALS_ENV_FILE`, the project root's `.env` (for an editable install), and `~/.deals-mcp/.env` (which also works for a normal install). The current directory is not used, since the client may start the server from anywhere. `DEFAULT_COUNTRY` must be a two-letter code. The wishlist is stored at `~/.deals-mcp/wishlist.db` unless `DEALS_DB_PATH` is set.

## Running and testing

```powershell
.venv\Scripts\python.exe -m pytest   # unit tests and in-process MCP tests (no network)
npx @modelcontextprotocol/inspector .venv\Scripts\python.exe -m deals_mcp.server   # MCP Inspector
```

The Inspector is used for calling tools, reading resources and rendering prompts by hand. The SDK's own `mcp dev` command is not used here, because it always launches the server through `uv`, which this setup does not need.

## Connecting a client

The server uses the stdio transport, so the client starts it as a subprocess.

**Claude Code:**

```powershell
claude mcp add deals --scope user -- C:\Users\mirkh\Code\mcp-project\.venv\Scripts\python.exe -m deals_mcp.server
```

Then run `/mcp` inside Claude Code in order to check that it is connected.

**Claude Desktop:** add the following to `%APPDATA%\Claude\claude_desktop_config.json` and restart the app:

```json
{
  "mcpServers": {
    "deals": {
      "command": "C:\\Users\\mirkh\\Code\\mcp-project\\.venv\\Scripts\\python.exe",
      "args": ["-m", "deals_mcp.server"]
    }
  }
}
```

## Deal alerts

An MCP server only runs while a client has it open, and it cannot message anyone by itself, so alerts come from a separate job, `python -m deals_mcp.alerts`, which is meant to run on a schedule. It uses the same wishlist and the same deal check as the `check_wishlist_deals` tool, and posts the deals to a Discord channel through a webhook.

Each deal is alerted once. A game is alerted again when its price drops, when a different sale or shop becomes the best deal, or when a sale that had ended comes back, and a deal that ends within 8 hours gets one "ending soon" reminder. A game that could not be checked (for example because a request failed) keeps its state, so a temporary failure never causes a repeat alert.

Setup:

1. In Discord, open the channel's settings, then Integrations, Webhooks, New Webhook, and copy its URL.
2. Add it to `.env` as `DISCORD_WEBHOOK_URL=...`. Anyone with this URL can post to the channel, so keep it out of git (`.env` is ignored).
3. Check it with `.venv\Scripts\python.exe -m deals_mcp.alerts --test`, and see what would be sent with `--dry-run`.
4. Schedule it with `.\scripts\register-alerts-task.ps1` (every 6 hours by default, `-Hours 3` to change it, `-Remove` to delete it). The task uses `pythonw.exe`, so no window opens, and it only runs while you are logged in to Windows.

Each run is logged to `~/.deals-mcp/alerts.log`. The command exits with 0 on success, 1 when the wishlist check fails, and 2 when Discord cannot be reached or the webhook is not set, which Task Scheduler shows as the last run result.

## Layout

- `src/deals_mcp/server.py`: the `MCPServer` instance along with every tool, resource and prompt.
- `src/deals_mcp/alerts.py`: the scheduled alert job and the Discord webhook sender.
- `src/deals_mcp/deal_check.py`: the wishlist check, shared by the MCP tool and the alert job.
- `src/deals_mcp/config.py`: settings from the environment and `.env` files.
- `src/deals_mcp/itad.py`: the IsThereAnyDeal API client.
- `src/deals_mcp/deal_logic.py`: pure functions for deciding whether a price is a deal.
- `src/deals_mcp/db.py`: the SQLite wishlist, and the record of which alerts were sent.
- `scripts/register-alerts-task.ps1`: registers the alert job with Windows Task Scheduler.
- `src/deals_mcp/matching.py`: title normalization and ranking for search results.
- `tests/fixtures/itad_search.json`: real ITAD search results for several franchise queries, so that the ranking tests run against actual titles without a network connection. Regenerate it if ITAD's catalogue changes enough to break those tests.

Logging goes to stderr, because stdout is used by the stdio transport, so a stray `print()` in the server will break the connection.
