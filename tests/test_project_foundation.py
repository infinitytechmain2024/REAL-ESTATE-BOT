"""Static and Docker-compatible checks for the VPS foundation."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_foundation_compose_renders_from_example_environment() -> None:
    result = subprocess.run(
        ["docker", "compose", "--env-file", ".env.example", "config"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    rendered = result.stdout
    for service in ("postgres:", "redis:", "caddy:"):
        assert service in rendered
    assert "postgres_data:" in rendered
    assert "redis_data:" in rendered
    assert "internal: true" in rendered


def test_migration_script_is_safe_and_tracks_the_orchestration_migration() -> None:
    script = ROOT / "scripts/apply_migrations.sh"
    text = script.read_text(encoding="utf-8")
    assert script.stat().st_mode & 0o111
    assert "003_orchestration.sql" in text
    assert "004_telegram_control_plane.sql" in text
    assert "005_orchestra_dispatcher.sql" in text
    assert "006_openrouter_transcription.sql" in text
    assert "007_live_view_sessions.sql" in text
    assert "008_analysis_pipeline.sql" in text
    assert "009_verification_flow.sql" in text
    assert "010_verification_telegram_identity.sql" in text
    assert "011_operator_access_requests.sql" in text
    assert "012_collector_launch_requests.sql" in text
    assert "013_analysis_claims.sql" in text
    assert "014_campaigns.sql" in text
    assert "015_campaign_groups.sql" in text
    assert "016_campaign_runs.sql" in text
    assert "017_control_settings.sql" in text
    assert "018_user_role_task_drafts.sql" in text
    assert "019_campaign_near_matches.sql" in text
    assert "020_campaign_excluded_findings.sql" in text
    assert "021_campaign_web_search.sql" in text
    assert "022_social_search.sql" in text
    assert "023_campaign_finding_relevance.sql" in text
    assert "024_agent_findings.sql" in text
    assert "025_agent_reductions.sql" in text
    assert "026_campaign_comment_leads.sql" in text
    assert "027_investor_reach.sql" in text
    assert "028_reach_company_kind.sql" in text
    assert "029_web_search_snippets.sql" in text
    assert "030_campaign_summary.sql" in text
    assert "031_task_draft_steps.sql" in text
    assert "032_web_seen_urls_ttl.sql" in text
    assert "033_campaign_finding_hold_reason.sql" in text
    assert "034_web_fetch_layers.sql" in text
    assert "035_campaign_specs.sql" in text
    assert "pg_advisory_xact_lock" in text
    assert "schema_migrations" in text
    assert "Refusing changed already-applied migration" in text
    assert "down -v" not in text


def test_gateway_and_hardening_notes_keep_internal_services_private() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    caddy = (ROOT / "docker/caddy/Caddyfile").read_text(encoding="utf-8")
    hardening = (ROOT / "docs/VPS_HARDENING.md").read_text(encoding="utf-8")
    assert "respond /healthz 200" in caddy
    assert "5432" not in compose.split("  redis:", 1)[0]
    assert "6379" not in compose.split("  caddy:", 1)[0]
    assert "Chrome CDP" in hardening
    assert "Tailscale" in hardening


def test_only_outbound_clients_join_the_egress_network() -> None:
    """Database/cache stay internal while Telegram and Chromium can reach HTTPS."""
    result = subprocess.run(
        ["docker", "compose", "--env-file", ".env.example", "config", "--format", "json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    rendered = json.loads(result.stdout)

    assert rendered["networks"]["backend"]["internal"] is True
    assert "internal" not in rendered["networks"]["egress"]
    assert set(rendered["services"]["postgres"]["networks"]) == {"backend"}
    assert set(rendered["services"]["redis"]["networks"]) == {"backend"}
    assert set(rendered["services"]["telegram"]["networks"]) == {"backend", "egress"}
    assert set(rendered["services"]["browser"]["networks"]) == {"backend", "egress"}
    assert "ports" not in rendered["services"]["telegram"]
    assert "ports" not in rendered["services"]["browser"]


def test_migration_script_lists_every_migration_by_its_real_path_in_order() -> None:
    """Both lists in apply_migrations.sh name every migration file, in order, and each apply entry is a real path."""
    import re

    text = (ROOT / "scripts/apply_migrations.sh").read_text(encoding="utf-8")
    on_disk = sorted(p.name for p in (ROOT / "bot/services/db/migrations").glob("[0-9][0-9][0-9]_*.sql"))
    required = re.search(r"for required in (.*?); do", text, re.DOTALL).group(1).split()
    applied = re.search(r"for migration_path in (.*?); do", text, re.DOTALL).group(1).replace("\\", " ").split()
    assert required == on_disk
    assert applied == [f"bot/services/db/migrations/{name}" for name in on_disk]
    assert all((ROOT / path).is_file() for path in applied)
