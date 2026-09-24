"""Contract tests for the database-owned orchestration state machines.

The repository's ordinary test environment intentionally has no Supabase
credentials or local PostgreSQL server. These tests validate the migration's
authoritative transition graph and its structural guards; CI/deployment should
also apply the migration with ``psql`` against a disposable PostgreSQL database.
"""

from __future__ import annotations

import re
from pathlib import Path

MIGRATION = (
    Path(__file__).parents[1] / "bot" / "services" / "db" / "migrations" / "003_orchestration.sql"
)


def _migration() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _transitions(entity: str) -> set[tuple[str, str]]:
    sql = _migration()
    match = re.search(
        rf"when '{entity}' then \(p_from, p_to\) in \((.*?)(?=\n      when |\n      else)",
        sql,
        flags=re.DOTALL,
    )
    assert match, f"missing transition graph for {entity}"
    return set(re.findall(r"\('([^']+)',\s*'([^']+)'\)", match.group(1)))


def test_required_tables_and_database_guards_are_present() -> None:
    sql = _migration()
    for table in (
        "monitoring_sources",
        "browser_profiles",
        "acquisition_batches",
        "acquisition_batch_items",
        "batch_runs",
        "acquisition_runs",
        "collected_posts",
        "collected_comments",
        "profile_extracts",
        "findings",
        "verification_jobs",
        "orchestration_audit_log",
    ):
        assert f"create table if not exists public.{table}" in sql

    # Every lifecycle table (audit is append-only, not lifecycle-managed) is
    # registered in the uniform state/audit trigger installation block.
    for table in (
        "monitoring_sources",
        "browser_profiles",
        "acquisition_batches",
        "acquisition_batch_items",
        "batch_runs",
        "acquisition_runs",
        "collected_posts",
        "collected_comments",
        "profile_extracts",
        "findings",
        "verification_jobs",
    ):
        assert f"'{table}'" in sql

    assert "acquisition_batch_items_one_active_idx" in sql
    assert "acquisition_runs_one_active_source_idx" in sql
    assert "verification_jobs_one_open_idx" in sql
    assert "reject_orchestration_audit_mutation" in sql
    assert "deleted_at" in sql
    assert "expires_at" in sql


def test_legal_and_illegal_source_transitions_are_declared() -> None:
    transitions = _transitions("monitoring_sources")
    assert ("draft", "active") in transitions
    assert ("active", "human_verification_required") in transitions
    assert ("human_verification_required", "active") in transitions
    assert ("active", "retired") in transitions
    assert ("retired", "active") not in transitions  # terminal is not reopened
    assert ("draft", "human_verification_required") not in transitions


def test_safety_critical_run_and_verification_transitions_are_declared() -> None:
    run = _transitions("acquisition_runs")
    assert ("queued", "running") in run
    assert ("running", "awaiting_human_verification") in run
    assert ("awaiting_human_verification", "queued") in run
    assert ("succeeded", "running") not in run
    assert ("failed", "running") not in run

    verification = _transitions("verification_jobs")
    assert ("requested", "active") in verification
    assert ("active", "verified") in verification
    assert ("verified", "active") not in verification


def test_browser_profile_and_batch_are_sequential_and_terminal() -> None:
    profile = _transitions("browser_profiles")
    assert ("ready", "in_use") in profile
    assert ("in_use", "human_verification_required") in profile
    assert ("retired", "ready") not in profile

    batch = _transitions("batch_runs")
    assert ("running", "human_verification_required") in batch
    assert ("human_verification_required", "queued") in batch
    assert ("succeeded", "running") not in batch
