"""Handler routers, in the order they are offered each update.

Order matters: `start` claims the commands and the mode buttons, `facebook_admin`
claims its own `/facebook` command and callback prefix (admin-only, silently
ignored otherwise), `callbacks` claims the result buttons, `voice` claims audio,
and `search` is last because its text handlers are the broadest. `errors` is
attached to the dispatcher separately.
"""

from aiogram import Router

from bot.handlers import callbacks, errors, facebook_admin, privacy, search, start, voice


def build_router() -> Router:
    """Assemble the single router the dispatcher includes."""
    root = Router(name="root")
    root.include_router(start.router)
    root.include_router(facebook_admin.router)
    root.include_router(privacy.router)
    root.include_router(callbacks.router)
    root.include_router(voice.router)
    root.include_router(search.router)
    return root


__all__ = ["build_router", "errors"]
