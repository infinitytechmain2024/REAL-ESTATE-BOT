"""Which Facebook links are a single post (light: the Telegram image reads it too, ``control_plane.intake``)."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

FB_HOSTS = frozenset({"facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com"})
_POST_PATH = re.compile(r"^/(?:groups/[^/]+/(?:posts|permalink)/[^/]+|[^/]+/posts/[^/]+)/?$")


def is_facebook_post(url: str | None) -> bool:
    """A single Facebook post (a group permalink or a page's /posts/): the only pages whose comments are read."""
    if not url:
        return False
    parts = urlsplit(url)
    return parts.scheme == "https" and (parts.hostname or "") in FB_HOSTS and bool(_POST_PATH.match(parts.path))
