"""Settings shared by the MCP server and the alert job, read from the environment and .env files."""

import os
import re
from pathlib import Path

from dotenv import load_dotenv
from mcp.server.mcpserver.exceptions import ToolError

DATA_DIR = Path.home() / ".deals-mcp"


def env_file_candidates() -> list[Path]:
    """Where .env files are read from, earliest first; a value already set is never overridden.

    The client may start the server from any directory, so the current directory is not used. The
    source tree's .env only exists for an editable install, so ~/.deals-mcp/.env also works for a
    normal install, and DEALS_ENV_FILE can point anywhere.
    """
    candidates = [Path(os.environ["DEALS_ENV_FILE"])] if os.getenv("DEALS_ENV_FILE") else []
    candidates.append(Path(__file__).resolve().parents[2] / ".env")
    candidates.append(DATA_DIR / ".env")
    return candidates


def load_env() -> None:
    for env_file in env_file_candidates():
        if env_file.is_file():
            load_dotenv(env_file)


def get_db_path() -> str:
    return os.getenv("DEALS_DB_PATH") or str(DATA_DIR / "wishlist.db")


def get_country(country: str | None) -> str:
    if country:
        return country.upper()
    default = (os.getenv("DEFAULT_COUNTRY") or "US").strip().upper()
    # Checked here because, unlike tool arguments, the environment is not validated, and a bad value
    # stored on wishlist rows would make every later price check for them fail.
    if not re.fullmatch(r"[A-Z]{2}", default):
        raise ToolError(f"DEFAULT_COUNTRY is set to '{default}', but it must be a two-letter country code such as US.")
    return default
