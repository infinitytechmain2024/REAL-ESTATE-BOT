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


# --- public websites (the web search's browser layer) ---------------------------------------
#
# A site that shows a person-check instead of its page: only recognised here, never solved or
# worked around. A real listing page is long; a challenge page is short, so every text signal
# needs a short page (a contact form's reCAPTCHA script on a long listing is no challenge).

WEBSITE_CHALLENGE_KINDS = ("captcha", "interstitial", "access_denied")
_CHALLENGE_FRAMES = ("recaptcha", "hcaptcha", "geetest", "arkoselabs", "funcaptcha", "captcha-delivery.com",
                     "px-captcha", "perimeterx", "datadome")
_INTERSTITIAL_FRAMES = ("challenges.cloudflare.com", "turnstile", "/cdn-cgi/challenge")
_INTERSTITIAL_TEXT = (
    "just a moment", "checking your browser", "checking if the site connection is secure", "verify you are human",
    "verifying you are human", "are you a robot", "are you human", "press & hold", "press and hold",
    "one more step", "attention required", "un momento", "comprobando su navegador", "verifica que eres humano",
    "no soy un robot", "eres una persona", "eres humano", "проверка браузера", "я не робот", "вы не робот",
)
_ACCESS_DENIED_TEXT = ("access denied", "acceso denegado", "request blocked", "you have been blocked", "доступ запрещ")
MAX_CHALLENGE_CHARS = 600   # a text signal counts on a page at most this long
MAX_FRAME_PAGE_CHARS = 500  # a captcha frame counts on a page at most this long
MAX_DENIED_CHARS = 300       # «Access denied» is a challenge only when that is nearly all the page says


def classify_website(snapshot: Mapping[str, object]) -> str | None:
    """``captcha`` / ``interstitial`` / ``access_denied`` when a browser snapshot of a public page is a challenge
    page, else None. ``frames``: the iframe and script sources the browser saw (``bot/browser_session``)."""
    body = " ".join(str(snapshot.get("text", "")).split())
    haystack = f"{snapshot.get('title', '')} {body}".lower()
    frames = " ".join(str(f) for f in (snapshot.get("frames") or []) if isinstance(f, str)).lower()  # type: ignore[attr-defined]
    url = str(snapshot.get("url", "")).lower()
    size = len(body)
    if size < MAX_FRAME_PAGE_CHARS:
        if any(m in frames or m in url for m in _INTERSTITIAL_FRAMES):
            return "interstitial"
        if any(m in frames or m in url for m in _CHALLENGE_FRAMES):
            return "captcha"
    if size < MAX_CHALLENGE_CHARS:
        if any(m in haystack for m in _INTERSTITIAL_TEXT):
            return "interstitial"
        kind, _ = classify(haystack)
        if kind == "captcha":
            return "captcha"
        if kind == "checkpoint":
            return "interstitial"
    if size < MAX_DENIED_CHARS and any(m in haystack for m in _ACCESS_DENIED_TEXT):
        return "access_denied"
    return None
