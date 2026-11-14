"""The bench's PostgreSQL: a local PostgreSQL 16 cluster under ``sim/pg`` with every migration, reset per run.

``python -m scripts.sim.db start`` creates (once) and starts the cluster on 127.0.0.1:54329 with trust auth and
prints its URL; ``stop`` stops it. A run resets the ``sim_test`` database (``reset``): the schemas are dropped, the
migrations of ``bot/services/db/migrations`` (the list of ``scripts/apply_migrations.sh``) are applied in order.

Determinism: before the migrations, ``sim.gen_random_uuid()`` (a sequence, not random) is put ahead of
``pg_catalog`` (pgcrypto lives in ``ext``) in the ``search_path`` of the migrations and of the run's connections (a
parameter of the run's URL), so the ids a replay creates are the ids the first pass created
(and the answers that quote them still fit). The same way ``sim.now()`` and ``sim.clock_timestamp()`` give the
database the runner's virtual clock: the real time plus the jumps of the run (``sim.clock``, ``set_clock``), so a
``finished_at`` stamped by a trigger is on the runner's time. ``SIM_DATABASE_URL`` uses another database instead (its name must end
in ``_test``: it is wiped).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "bot" / "services" / "db" / "migrations"
PORT = 54329
DATABASE = "sim_test"
DEFAULT_URL = f"postgresql://postgres@127.0.0.1:{PORT}/{DATABASE}"


def _bin(name: str) -> str:
    for folder in ("/usr/lib/postgresql/16/bin", "/usr/local/pgsql/bin"):
        if Path(folder, name).is_file():
            return str(Path(folder, name))
    found = shutil.which(name)
    if not found:
        raise SystemExit(f"{name} not found: install PostgreSQL 16 or set SIM_DATABASE_URL")
    return found


def _as_postgres(args: list[str]) -> list[str]:
    """initdb and the server refuse root: run them as the ``postgres`` user then."""
    if os.geteuid() == 0:
        return ["runuser", "-u", "postgres", "--", *args]
    return args


def migrations() -> list[Path]:
    """The migrations in the order ``scripts/apply_migrations.sh`` applies them (numbered files, sorted)."""
    return sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql"))


def start(sim_root: Path) -> str:
    folder = sim_root / "pg"
    data = folder / "data"
    folder.mkdir(parents=True, exist_ok=True)
    if os.geteuid() == 0:
        shutil.chown(folder, "postgres", "postgres")
    if not (data / "PG_VERSION").is_file():
        subprocess.run(_as_postgres([_bin("initdb"), "-D", str(data), "-U", "postgres", "--auth=trust", "-E", "UTF8",
                                     "--locale=C.UTF-8"]), check=True, stdout=subprocess.DEVNULL)
    status = subprocess.run(_as_postgres([_bin("pg_ctl"), "-D", str(data), "status"]), capture_output=True)
    if status.returncode != 0:
        subprocess.run(_as_postgres([_bin("pg_ctl"), "-D", str(data), "-l", str(folder / "server.log"), "-w", "-o",
                                     f"-p {PORT} -k {folder} -c listen_addresses=127.0.0.1", "start"]),
                       check=True, stdout=subprocess.DEVNULL)
    exists = subprocess.run([_bin("psql"), "-h", "127.0.0.1", "-p", str(PORT), "-U", "postgres", "-tAc",
                             f"select 1 from pg_database where datname = '{DATABASE}'"], capture_output=True, text=True,
                            check=True)
    if exists.stdout.strip() != "1":
        subprocess.run([_bin("createdb"), "-h", "127.0.0.1", "-p", str(PORT), "-U", "postgres", DATABASE], check=True)
    return DEFAULT_URL


def stop(sim_root: Path) -> None:
    data = sim_root / "pg" / "data"
    if (data / "PG_VERSION").is_file():
        subprocess.run(_as_postgres([_bin("pg_ctl"), "-D", str(data), "-m", "fast", "stop"]), check=False)


def url(sim_root: Path) -> str:
    """``SIM_DATABASE_URL`` when set (a ``*_test`` database), else the local cluster (started if needed)."""
    given = os.environ.get("SIM_DATABASE_URL", "")
    if given:
        if not given.split("?")[0].rstrip("/").endswith("_test"):
            raise SystemExit("SIM_DATABASE_URL must name a disposable *_test database: every run wipes it")
        return given
    return start(sim_root)


RESET = """
drop schema if exists public cascade;
drop schema if exists sim cascade;
drop extension if exists pgcrypto;
drop schema if exists ext cascade;
create schema public;
create schema ext;
create extension pgcrypto schema ext;
create schema sim;
create sequence sim.uuid_seq;
create function sim.gen_random_uuid() returns uuid language sql volatile as
  $$ select ('00000000-0000-4000-8000-' || lpad(to_hex(nextval('sim.uuid_seq')), 12, '0'))::uuid $$;
create table sim.clock (shift_seconds double precision not null);
insert into sim.clock values (0);
create function sim.now() returns timestamptz language sql stable as
  $$ select pg_catalog.now() + make_interval(secs => (select shift_seconds from sim.clock)) $$;
create function sim.clock_timestamp() returns timestamptz language sql volatile as
  $$ select pg_catalog.clock_timestamp() + make_interval(secs => (select shift_seconds from sim.clock)) $$;
"""


SEARCH_PATH = "public,sim,ext,pg_catalog"


def with_search_path(database_url: str) -> str:
    """The run's URL: every connection of the bot resolves ``gen_random_uuid`` to ``sim`` (asyncpg passes an unknown
    query parameter as a server setting); the database itself is not altered."""
    return f"{database_url}{'&' if '?' in database_url else '?'}search_path={SEARCH_PATH.replace(',', '%2C')}"


async def set_clock(pool: Any, shift: timedelta) -> None:
    """The database's ``now()`` is the real time plus ``shift`` (the runner's clock offset)."""
    await pool.execute("update sim.clock set shift_seconds = $1", shift.total_seconds())


async def reset(database_url: str) -> str:
    """A clean database with every migration, ids from a sequence; returns the URL the run must use."""
    import asyncpg

    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute(RESET)
        await conn.execute(f"set search_path = {SEARCH_PATH}")
        for path in migrations():
            await conn.execute(path.read_text(encoding="utf-8"))
    finally:
        await conn.close()
    return with_search_path(database_url)


async def restore(database_url: str) -> None:
    """Leave a shared ``*_test`` database as the other Postgres tests expect it: an empty ``public``, no ``sim``."""
    import asyncpg

    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute("drop schema if exists public cascade; drop schema if exists sim cascade; "
                           "drop extension if exists pgcrypto; drop schema if exists ext cascade; create schema public;")
    finally:
        await conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.sim.db", description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["start", "stop", "url"])
    parser.add_argument("--sim", default=str(ROOT / "sim"), help="the bench folder (default: <repo>/sim)")
    args = parser.parse_args(argv)
    sim_root = Path(args.sim).resolve()
    if args.action == "stop":
        stop(sim_root)
        return 0
    print(url(sim_root) if args.action == "url" else start(sim_root))
    return 0


if __name__ == "__main__":
    sys.exit(main())
