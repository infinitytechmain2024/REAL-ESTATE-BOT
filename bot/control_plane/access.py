"""Access requests from Telegram, decided by an owner, with a role.

Someone without access gets a "Запросить доступ" (request access) button. The owners
(TELEGRAM_OPERATOR_IDS in .env) receive the request with "Approve as helper",
"Approve as user", "Approve as operator" and "Deny" buttons. A helper only
handles human verification (log in, CAPTCHA, checkpoint); a user picks a mode
and gives search tasks, each launched with a button (bot/control_plane/intake.py);
an operator also controls collection. An approval takes effect at once in the
shared OperatorSet and is stored (migrations 011, 018). Only owners decide,
list, change roles and revoke.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from bot.control_plane.models import Button, Reply
from bot.operators import ROLES, OperatorSet

log = logging.getLogger(__name__)
DENY_COOLDOWN = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class AccessRequest:
    id: str
    user_id: int
    display_name: str | None
    username: str | None
    state: str


class AccessStore(Protocol):
    async def approved_roles(self) -> dict[int, str]: ...
    async def open_request(self, user_id: int, display_name: str | None, username: str | None, cooldown: timedelta) -> tuple[AccessRequest | None, str]: ...
    async def decide(self, request_id: str, role: str | None, owner_id: int) -> AccessRequest | None: ...
    async def set_role(self, user_id: int, role: str) -> bool: ...
    async def revoke(self, user_id: int, owner_id: int) -> bool: ...
    async def operators(self) -> list[tuple[int, str | None, str | None, str]]: ...


ROLE_TEXT = {
    "helper": "helper (verification only: log in, CAPTCHA, checkpoint)",
    "user": "user (Пользователь: choose a mode, describe a search task, press Запустить)",
    "operator": "operator (verification and control of collection)",
}


ROLE_RU = {"user": "Пользователь", "helper": "Помощник", "operator": "Оператор"}
ROLE_CHANGED = {
    "user": "Ваш доступ изменён: теперь вы пользователь. Нажмите /start, выберите режим и опишите задачу.",
    "helper": ("Ваш доступ изменён: теперь вы помощник. Когда для входа в Facebook понадобится человек, "
               "я пришлю сообщение с кнопкой."),
    "operator": "Ваш доступ изменён: теперь вы оператор. Отправьте /help, чтобы увидеть команды.",
}
SETTINGS_BUTTON = Button("⚙️ Настройки", callback_data="set:list")


USER_WELCOME = ("Доступ открыт: вы пользователь. Нажмите /start, выберите режим и опишите задачу — "
                "бот уточнит детали и попросит подтвердить запуск кнопкой «Запустить».")


def label(display_name: str | None, username: str | None, user_id: int) -> str:
    who = display_name or "Unknown"
    return f"{who}{f' (@{username})' if username else ''}, ID {user_id}"


class AccessDesk:
    def __init__(self, store: AccessStore, operators: OperatorSet, notify: Callable[[int, Reply], Awaitable[None]] | None = None) -> None:
        self.store, self.operators, self.notify = store, operators, notify

    @staticmethod
    def button() -> Button:
        return Button("Запросить доступ", callback_data="access:request")

    async def load(self) -> None:
        self.operators.replace_approved(await self.store.approved_roles())

    async def request(self, user_id: int | None, display_name: str | None, username: str | None) -> Reply:
        if user_id is None:
            return Reply("Access can only be requested from a Telegram account.")
        if self.operators.has_access(user_id):
            return Reply("You already have access.")
        request, status = await self.store.open_request(user_id, display_name, username, DENY_COOLDOWN)
        if status == "pending":
            return Reply("Your request is already waiting for an owner's decision.")
        if status == "recently_denied" or request is None:
            return Reply("Your last request was declined. You can ask again later.")
        text = (
            f"Access request: {label(display_name, username, user_id)}.\n\n"
            "A helper only gets verification tasks (log in, CAPTCHA, checkpoint); an operator can also start, "
            "pause and cancel collection. Both open the browser logged into the Facebook account, so approve "
            "only people you trust. A user (Пользователь) only picks a mode and gives search tasks, each "
            "confirmed with a button; no browser, no verification, no control. Everything else keeps coming "
            "to owners only."
        )
        buttons = (
            Button("Approve as helper", callback_data=f"access:helper:{request.id}"),
            Button("Approve as user (Пользователь)", callback_data=f"access:user:{request.id}"),
            Button("Approve as operator", callback_data=f"access:operator:{request.id}"),
            Button("Deny", callback_data=f"access:deny:{request.id}"),
        )
        sent = 0
        for owner in sorted(self.operators.owners):
            if self.notify is None:
                break
            try:
                await self.notify(owner, Reply(text, buttons))
                sent += 1
            except Exception:  # noqa: BLE001 - one owner who never started the bot must not stop the rest
                log.warning("telegram.access.notify_failed", extra={"owner": owner})
        log.info("telegram.access.requested", extra={"user_id": user_id, "owners_notified": sent})
        return Reply("Request sent. You will get a message when an owner decides.")

    async def decide(self, owner_id: int | None, request_id: str, role: str | None) -> Reply:
        """``role`` is ``helper``, ``user`` or ``operator`` to approve, ``None`` to deny."""
        if not self.operators.is_owner(owner_id):
            return Reply("Only an owner can decide access requests.")
        if not _is_uuid(request_id) or (role is not None and role not in ROLES):
            return Reply("This button is no longer valid.")
        decided = await self.store.decide(request_id, role, owner_id)  # type: ignore[arg-type]
        if decided is None:
            return Reply("This request was already decided.")
        who = label(decided.display_name, decided.username, decided.user_id)
        if role is not None:
            self.operators.set(decided.user_id, role)
        log.info("telegram.access.decided", extra={"user_id": decided.user_id, "role": role, "owner": owner_id})
        await self._tell(decided.user_id, (
            USER_WELCOME if role == "user"
            else f"Access granted as {ROLE_TEXT[role]}. Send /help to see what you can do." if role
            else "Your access request was declined."))
        return Reply(f"Approved as {role}: {who}." if role else f"Denied: {who}.")

    async def set_role(self, owner_id: int | None, argument: str) -> Reply:
        if not self.operators.is_owner(owner_id):
            return Reply("Only an owner can change roles.")
        parts = argument.split()
        if len(parts) != 2 or not parts[0].isdigit() or parts[1].lower() not in ROLES:
            return Reply("Use: /role <Telegram user ID> helper|user|operator")
        user_id, role = int(parts[0]), parts[1].lower()
        if user_id in self.operators.owners:
            return Reply("Owners are set in .env (TELEGRAM_OPERATOR_IDS) and always have full access.")
        if not await self._apply_role(user_id, role):
            return Reply(f"{user_id} is not an approved person.")
        return Reply(f"{user_id} is now a {role}.")

    async def _apply_role(self, user_id: int, role: str) -> bool:
        if not await self.store.set_role(user_id, role):
            return False
        self.operators.set(user_id, role)
        log.info("telegram.access.role_changed", extra={"user_id": user_id, "role": role})
        await self._tell(user_id, ROLE_CHANGED[role])
        return True

    async def _tell(self, user_id: int, text: str) -> None:
        if self.notify is None:
            return
        try:
            await self.notify(user_id, Reply(text))
        except Exception:  # noqa: BLE001 - the decision stands even if the message fails
            log.warning("telegram.access.reply_failed", extra={"user_id": user_id})

    async def list(self, owner_id: int | None) -> Reply:
        if not self.operators.is_owner(owner_id):
            return Reply("Only an owner can list operators.")
        rows = await self.store.operators()
        owners = ", ".join(str(i) for i in sorted(self.operators.owners))
        approved = "\n".join(f"- {label(name, username, uid)}: {role}" for uid, name, username, role in rows) or "- none"
        return Reply(f"Owners (from .env): {owners}\nApproved:\n{approved}\n\nChange with /role <ID> helper|user|operator, remove with /revoke <ID>.")

    async def revoke(self, owner_id: int | None, argument: str) -> Reply:
        if not self.operators.is_owner(owner_id):
            return Reply("Only an owner can revoke access.")
        if not argument.strip().isdigit():
            return Reply("Use: /revoke <Telegram user ID>")
        user_id = int(argument.strip())
        if user_id in self.operators.owners:
            return Reply("Owners are set in .env (TELEGRAM_OPERATOR_IDS) and cannot be revoked here.")
        if not await self._apply_revoke(user_id, owner_id):  # type: ignore[arg-type]
            return Reply(f"{user_id} is not an approved operator.")
        return Reply(f"Revoked: {user_id}.")

    async def _apply_revoke(self, user_id: int, owner_id: int) -> bool:
        if not await self.store.revoke(user_id, owner_id):
            return False
        self.operators.discard(user_id)
        log.info("telegram.access.revoked", extra={"user_id": user_id, "owner": owner_id})
        await self._tell(user_id, "Your access was revoked.")
        return True

    # --- settings panel: the owner changes roles with buttons (``set:...`` callbacks) ---------

    async def settings(self, owner_id: int | None, action: str = "list", target: str = "") -> Reply:
        """``set:list``, ``set:user:<id>``, ``set:role:<id>:<role>``, ``set:del:<id>``, ``set:delok:<id>``."""
        if not self.operators.is_owner(owner_id):
            return Reply("Настройки доступны только владельцу.")
        if action == "list":
            return await self._settings_list()
        uid_text, _, role = target.partition(":")
        if not uid_text.isdigit() or int(uid_text) in self.operators.owners:
            return Reply("Эта кнопка устарела.", (SETTINGS_BUTTON,))
        user_id = int(uid_text)
        if action == "role" and role in ROLES:
            await self._apply_role(user_id, role)
        elif action == "del":
            person = await self._person(user_id)
            if person is None:
                return await self._settings_list()
            return Reply(f"Удалить доступ для {person[0]}? Человек сможет запросить доступ заново.", (
                Button("🗑 Да, удалить", callback_data=f"set:delok:{user_id}"),
                Button("Отмена", callback_data=f"set:user:{user_id}"),
            ))
        elif action == "delok":
            removed = await self._apply_revoke(user_id, owner_id)  # type: ignore[arg-type]
            listing = await self._settings_list()
            return Reply(("Доступ удалён.\n\n" if removed else "") + listing.text, listing.buttons)
        elif action != "user":
            return Reply("Эта кнопка устарела.", (SETTINGS_BUTTON,))
        return await self._settings_card(user_id)

    async def _person(self, user_id: int) -> tuple[str, str] | None:
        for uid, name, username, role in await self.store.operators():
            if uid == user_id:
                return label(name, username, uid), role
        return None

    async def _settings_list(self) -> Reply:
        rows = await self.store.operators()
        if not rows:
            return Reply("⚙️ Настройки\n\nОдобренных аккаунтов пока нет.")
        buttons = tuple(
            Button(f"{name or username or uid} — {ROLE_RU.get(role, role)}", callback_data=f"set:user:{uid}")
            for uid, name, username, role in rows
        )
        return Reply("⚙️ Настройки\n\nВыберите аккаунт, чтобы сменить его роль или удалить доступ.", buttons)

    async def _settings_card(self, user_id: int) -> Reply:
        person = await self._person(user_id)
        if person is None:
            return await self._settings_list()
        who, current = person
        buttons = tuple(
            Button(("✓ " if role == current else "") + ROLE_RU[role], callback_data=f"set:role:{user_id}:{role}")
            for role in ("user", "helper", "operator")
        )
        return Reply(
            f"{who}\nСейчас: {ROLE_RU.get(current, current)}.\n\n"
            "Пользователь — выбирает режим и даёт задачи на поиск.\n"
            "Помощник — только проходит проверки Facebook (капча, вход).\n"
            "Оператор — задачи, проверки и управление сбором.",
            (*buttons, Button("🗑 Удалить доступ", callback_data=f"set:del:{user_id}"),
             Button("← Все аккаунты", callback_data="set:list")),
        )


def _is_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


class PostgresAccessStore:
    """Shares the control plane's pool."""

    def __init__(self, pool_owner: Any) -> None:
        self._owner = pool_owner

    def _pool(self) -> Any:
        return self._owner._pool()

    async def approved_roles(self) -> dict[int, str]:
        rows = await self._pool().fetch("select telegram_user_id, role from public.telegram_operators where state = 'approved'")
        return {r[0]: r[1] for r in rows}

    async def open_request(self, user_id: int, display_name: str | None, username: str | None, cooldown: timedelta) -> tuple[AccessRequest | None, str]:
        async with self._pool().acquire() as conn, conn.transaction():
            if await conn.fetchval("select 1 from public.telegram_access_requests where telegram_user_id = $1 and state = 'pending'", user_id):
                return None, "pending"
            if await conn.fetchval(
                """select 1 from public.telegram_access_requests
                    where telegram_user_id = $1 and state = 'denied' and decided_at > now() - ($2 * interval '1 second')""",
                user_id, int(cooldown.total_seconds()),
            ):
                return None, "recently_denied"
            row = await conn.fetchrow(
                """insert into public.telegram_access_requests (telegram_user_id, display_name, username)
                   values ($1, $2, $3) on conflict do nothing
                   returning id::text, telegram_user_id, display_name, username, state""",
                user_id, (display_name or "")[:128] or None, (username or "")[:64] or None,
            )
        return (AccessRequest(*row), "created") if row else (None, "pending")

    async def decide(self, request_id: str, role: str | None, owner_id: int) -> AccessRequest | None:
        async with self._pool().acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """update public.telegram_access_requests set state = $2, decided_at = now(), decided_by = $3
                    where id = $1::uuid and state = 'pending'
                returning id::text, telegram_user_id, display_name, username, state""",
                request_id, "approved" if role else "denied", owner_id,
            )
            if row and role:
                await conn.execute(
                    """insert into public.telegram_operators
                         (telegram_user_id, display_name, username, state, role, approved_by, approved_at, request_id)
                       values ($1, $2, $3, 'approved', $4, $5, now(), $6::uuid)
                       on conflict (telegram_user_id) do update set
                         display_name = excluded.display_name, username = excluded.username, state = 'approved',
                         role = excluded.role, approved_by = excluded.approved_by, approved_at = now(),
                         revoked_by = null, revoked_at = null, request_id = excluded.request_id""",
                    row["telegram_user_id"], row["display_name"], row["username"], role, owner_id, request_id,
                )
        return AccessRequest(*row) if row else None

    async def set_role(self, user_id: int, role: str) -> bool:
        return bool(await self._pool().fetchval(
            "update public.telegram_operators set role = $2 where telegram_user_id = $1 and state = 'approved' returning 1",
            user_id, role,
        ))

    async def revoke(self, user_id: int, owner_id: int) -> bool:
        return bool(await self._pool().fetchval(
            """update public.telegram_operators set state = 'revoked', revoked_by = $2, revoked_at = now()
                where telegram_user_id = $1 and state = 'approved' returning 1""",
            user_id, owner_id,
        ))

    async def operators(self) -> list[tuple[int, str | None, str | None, str]]:
        rows = await self._pool().fetch(
            "select telegram_user_id, display_name, username, role from public.telegram_operators where state = 'approved' order by approved_at"
        )
        return [(r[0], r[1], r[2], r[3]) for r in rows]


class MemoryAccessStore:
    def __init__(self) -> None:
        self.requests: dict[str, AccessRequest] = {}
        self.decided_at: dict[str, datetime] = {}
        self.approved: dict[int, tuple[str | None, str | None, str]] = {}

    async def approved_roles(self) -> dict[int, str]:
        return {uid: row[2] for uid, row in self.approved.items()}

    async def open_request(self, user_id: int, display_name: str | None, username: str | None, cooldown: timedelta) -> tuple[AccessRequest | None, str]:
        mine = [r for r in self.requests.values() if r.user_id == user_id]
        if any(r.state == "pending" for r in mine):
            return None, "pending"
        if any(r.state == "denied" and self.decided_at[r.id] > datetime.now(UTC) - cooldown for r in mine):
            return None, "recently_denied"
        request = AccessRequest(str(uuid.uuid4()), user_id, display_name, username, "pending")
        self.requests[request.id] = request
        return request, "created"

    async def decide(self, request_id: str, role: str | None, owner_id: int) -> AccessRequest | None:
        request = self.requests.get(request_id)
        if request is None or request.state != "pending":
            return None
        self.requests[request_id] = replace(request, state="approved" if role else "denied")
        self.decided_at[request_id] = datetime.now(UTC)
        if role:
            self.approved[request.user_id] = (request.display_name, request.username, role)
        return self.requests[request_id]

    async def set_role(self, user_id: int, role: str) -> bool:
        if user_id not in self.approved:
            return False
        name, username, _ = self.approved[user_id]
        self.approved[user_id] = (name, username, role)
        return True

    async def revoke(self, user_id: int, owner_id: int) -> bool:
        return self.approved.pop(user_id, None) is not None

    async def operators(self) -> list[tuple[int, str | None, str | None, str]]:
        return [(uid, name, username, role) for uid, (name, username, role) in self.approved.items()]
