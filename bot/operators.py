"""Who may do what: owners from .env plus people they approved, with a role.

- Owners (TELEGRAM_OPERATOR_IDS) can do everything, are the only ones who
  approve, change or revoke others, and alone receive administrative notices.
- ``operator``: controls collection (run, pause, resume, cancel, voice
  commands, detailed status) and handles verification.
- ``helper``: handles human verification only -- log in, CAPTCHA,
  checkpoint -- and nothing else.

Approved people live in the database (migration 011) and change at runtime.
``user_id in operators`` means "may handle verification" (every role);
``operators.controllers`` is the narrower set allowed to change state.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Iterator, Mapping

ROLES = ("helper", "operator")


class _Controllers(Collection[int]):
    def __init__(self, parent: OperatorSet) -> None:
        self._parent = parent

    def __contains__(self, user_id: object) -> bool:
        return isinstance(user_id, int) and self._parent.can_control(user_id)

    def __iter__(self) -> Iterator[int]:
        return iter(sorted(u for u in self._parent if self._parent.can_control(u)))

    def __len__(self) -> int:
        return sum(1 for _ in self)


class OperatorSet(Collection[int]):
    def __init__(self, owners: Iterable[int], approved: Mapping[int, str] | None = None) -> None:
        self.owners = frozenset(owners)
        self._roles: dict[int, str] = dict(approved or {})
        self.controllers = _Controllers(self)

    def is_owner(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.owners

    def role(self, user_id: int | None) -> str | None:
        if user_id is None:
            return None
        if user_id in self.owners:
            return "owner"
        return self._roles.get(user_id)

    def can_control(self, user_id: int | None) -> bool:
        return self.role(user_id) in {"owner", "operator"}

    @property
    def approved(self) -> dict[int, str]:
        return {u: r for u, r in self._roles.items() if u not in self.owners}

    def replace_approved(self, roles: Mapping[int, str]) -> None:
        self._roles = dict(roles)

    def set(self, user_id: int, role: str) -> None:
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        self._roles[user_id] = role

    def discard(self, user_id: int) -> None:
        self._roles.pop(user_id, None)

    def __contains__(self, user_id: object) -> bool:
        return user_id in self.owners or user_id in self._roles

    def __iter__(self) -> Iterator[int]:
        return iter(sorted(self.owners | set(self._roles)))

    def __len__(self) -> int:
        return len(self.owners | set(self._roles))

    def __repr__(self) -> str:
        return f"OperatorSet(owners={sorted(self.owners)}, approved={dict(sorted(self.approved.items()))})"
