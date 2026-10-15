"""Container health for campaign-runner: the runner imports and PostgreSQL answers ``select 1`` (5 s)."""

from bot.campaign import runner  # noqa: F401
from bot.utils.health import exit_code_for_database

if __name__ == "__main__":
    raise SystemExit(exit_code_for_database())
