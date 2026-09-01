"""Handler routers, in the order they are offered each update.

Order matters: `start` claims the commands and the mode buttons, `callbacks`
claims the result buttons, `voice` claims audio, and `search` is last because
its text handlers are the broadest. `errors` is attached to the dispatcher
separately.
"""

from aiogram import Router

from bot.handlers import callbacks, errors, search, start, voice


def build_router() -> Router:
    """Assemble the single router the dispatcher includes."""
    root = Router(name="root")
    root.include_router(start.router)
    root.include_router(callbacks.router)
    root.include_router(voice.router)
    root.include_router(search.router)
    return root


__all__ = ["build_router", "errors"]
