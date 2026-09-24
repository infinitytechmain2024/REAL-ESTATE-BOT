from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest

from bot.config import SupabaseSettings
from bot.services.db.supabase_repo import SupabaseRepository
from bot.services.facebook.browser import SessionState
from tests.conftest import FakeIncidentRepo, FakeSession
from tests.test_facebook_watchdog import make_watchdog


@pytest.mark.parametrize("existing", [False, True])
async def test_restart_restores_incident_without_alerting_again(existing):
    incident_id = uuid4()
    repo = FakeIncidentRepo({"id": str(incident_id)} if existing else None)
    watchdog, bot, _ = make_watchdog(FakeSession([SessionState.LOGIN_NEEDED, SessionState.HEALTHY]))
    watchdog.repo = repo
    await watchdog.tick()
    assert len(bot.messages) == (0 if existing else 1)
    assert len(repo.opened) == (0 if existing else 1)
    await watchdog.tick()
    assert "Готово" in bot.messages[-1]
    assert len(repo.resolved) == 1
    if existing:
        assert str(repo.resolved[0]) == str(incident_id)


async def test_repository_disabled_is_safe():
    repo = SupabaseRepository(SupabaseSettings(url=None, key=None))
    assert await repo.current_facebook_incident() is None
    assert await repo.open_facebook_incident("login_needed") is None
    await repo.resolve_facebook_incident(uuid4())
    watchdog, bot, _ = make_watchdog(FakeSession([SessionState.LOGIN_NEEDED] * 2))
    watchdog.repo = repo
    await watchdog.tick()
    await watchdog.tick()
    assert len(bot.messages) == 1


async def test_repository_uses_existing_table_and_scoped_queries():
    repo = SupabaseRepository(SupabaseSettings(url=None, key=None))
    query = Mock()
    for method in ["select", "insert", "update", "is_", "eq", "order", "limit"]:
        getattr(query, method).return_value = query
    incident_id = uuid4()
    query.execute = AsyncMock(return_value=Mock(data=[{"id": str(incident_id)}]))
    repo._client = Mock(table=Mock(return_value=query))
    assert await repo.open_facebook_incident("login_needed") == incident_id
    query.insert.assert_called_once_with({"state": "login_needed"})
    assert await repo.current_facebook_incident() == {"id": str(incident_id)}
    query.is_.assert_called_with("resolved_at", "null")
    query.order.assert_called_once_with("detected_at", desc=True)
    query.limit.assert_called_once_with(1)
    await repo.resolve_facebook_incident(incident_id)
    query.eq.assert_called_with("id", str(incident_id))
    assert query.update.call_args.args[0]["resolved_at"]
    assert all(
        call.args == ("facebook_session_incidents",) for call in repo._client.table.call_args_list
    )
    query.execute.side_effect = RuntimeError("offline")
    assert await repo.current_facebook_incident() is None
    assert await repo.open_facebook_incident("login_needed") is None
    await repo.resolve_facebook_incident(UUID(str(incident_id)))
