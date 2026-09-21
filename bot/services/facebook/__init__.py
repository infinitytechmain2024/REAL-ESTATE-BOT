"""Facebook group reading: browser session, access classification, post/comment extraction.

See ``browser.py`` for the shared persistent session, ``groups.py`` for
group-level operations, ``client.py`` for the bridge into the research
pipeline's ``SearchHit`` shape, ``tokens.py`` for the remote live-view access
token, and ``gate.py`` for the token-gated proxy in front of noVNC. Joining is
limited to a normal public Join button; challenges remain human-only.
"""

from bot.services.facebook.browser import FacebookSession, SessionState
from bot.services.facebook.client import FacebookSource
from bot.services.facebook.gate import build_gate_app
from bot.services.facebook.groups import (
    GroupAccess,
    GroupComment,
    GroupPost,
    check_access,
    search_posts,
)
from bot.services.facebook.tokens import TokenStore

__all__ = [
    "FacebookSession",
    "FacebookSource",
    "GroupAccess",
    "GroupComment",
    "GroupPost",
    "SessionState",
    "TokenStore",
    "build_gate_app",
    "check_access",
    "search_posts",
]
