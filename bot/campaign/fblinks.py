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


def post_link_only(campaign: object) -> bool:
    """An investor campaign whose whole task is Facebook post links (``spec.post_link``, set by the intake): it only
    reads their comments (``post_leads``); discovery, the web stage, the reach and the social search skip it."""
    spec = getattr(campaign, "spec", None) or {}
    required = (spec.get("sources") or {}).get("required") if isinstance(spec, dict) else None
    plan = getattr(campaign, "plan", None)
    return (getattr(plan, "vertical", None) == "investors" and isinstance(spec, dict) and spec.get("post_link") is True
            and isinstance(required, list) and bool(required) and all(is_facebook_post(u) for u in required))
