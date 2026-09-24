"""Name a challenge, and decide whether it is the owner's alone.

Sensitive kinds -- identity verification, new two-factor enrolment, and
account restrictions -- are about the account itself rather than one session.
The flow never opens a browser for them: it stops everything on that profile
and tells the owner. Everything is matched on lowercase text, and sensitive
signals are checked first so that a page mentioning both is treated as the
more serious one.
"""

from __future__ import annotations

from collections.abc import Mapping

SENSITIVE_KINDS = frozenset({"identity_verification", "two_factor_setup", "account_restricted"})

_SIGNALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("identity_verification", (
        "confirm your identity", "verify your identity", "identity verification", "photo of your id",
        "upload a photo of yourself", "video selfie", "government id", "sube una foto", "confirma tu identidad",
        "подтвердите свою личность", "подтвердите личность", "підтвердіть свою особу",
    )),
    ("two_factor_setup", (
        "turn on two-factor", "set up two-factor", "two-factor authentication required",
        "activa la autenticación en dos pasos", "включите двухфакторную", "увімкніть двофакторну",
    )),
    ("account_restricted", (
        "account disabled", "account restricted", "account has been suspended", "we suspended your account",
        "your account is restricted", "account has been locked", "your account has been disabled",
        "/disabled", "cuenta inhabilitada", "cuenta suspendida", "аккаунт отключ", "аккаунт заблокирован",
        "обліковий запис вимкнено",
    )),
    ("captcha", ("captcha", "introduce los caracteres")),
    ("login", ("/login", "log in to facebook", "log into facebook")),
    ("account_warning", ("we detected automated behavior", "review your account")),
    ("checkpoint", (
        "/checkpoint", "/two_factor", "/recover", "/security/", "security check", "confirm it's you",
        "confirma que eres", "suspicious activity", "unusual activity", "actividad sospechosa",
    )),
)


def classify(*texts: str | None) -> tuple[str, bool]:
    """Return ``(kind, sensitive)`` for challenge text, URLs or collector reasons."""
    haystack = "\n".join(t for t in texts if t).lower()
    for kind, signals in _SIGNALS:
        if any(signal in haystack for signal in signals):
            return kind, kind in SENSITIVE_KINDS
    return "unknown", False


def classify_snapshot(snapshot: Mapping[str, object]) -> tuple[str | None, bool]:
    """Classify a browser snapshot; ``(None, False)`` when it shows no challenge."""
    kind, sensitive = classify(str(snapshot.get("url", "")), str(snapshot.get("title", "")), str(snapshot.get("text", ""))[:20000])
    return (None, False) if kind == "unknown" else (kind, sensitive)
