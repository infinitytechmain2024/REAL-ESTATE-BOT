"""URL normalisation and hashing.

De-duplication is only as good as the normalisation in front of it: the same
listing routinely appears as ``http://Example.com/a/?utm_source=fb`` and
``https://example.com/a#gallery``. :func:`normalize_url` collapses those to one
canonical string and :func:`url_hash` turns it into the ``url_hash`` column
that carries the ``UNIQUE (user_id, url_hash)`` constraint.
"""

from __future__ import annotations

import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query parameters that never change which document is served.
_TRACKING_PREFIXES: tuple[str, ...] = ("utm_", "pk_", "mc_", "ga_", "_hs")
_TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "gbraid",
        "wbraid",
        "yclid",
        "msclkid",
        "igshid",
        "mkt_tok",
        "ref",
        "ref_src",
        "referrer",
        "source",
        "spm",
        "trk",
        "cmpid",
        "campaign",
        "_openstat",
    }
)

_DEFAULT_PORTS: dict[str, str] = {"http": "80", "https": "443"}


def normalize_url(url: str) -> str:
    """Return a canonical form of *url* for comparison purposes.

    Lower-cases the host, drops ``www.``, the fragment, default ports,
    tracking parameters and a trailing slash, and sorts the remaining query
    parameters. ``http`` is folded into ``https`` because the two serve the
    same document and we only ever want to show a listing once -- the original
    URL is what actually gets fetched and displayed, this form exists purely
    for comparison. Input without a host (or that cannot be parsed at all) is
    returned stripped, so this never raises on engine-supplied data.
    """
    candidate = (url or "").strip()
    if not candidate:
        return ""

    try:
        parts = urlsplit(candidate)
    except ValueError:
        return candidate

    host = parts.hostname or ""
    if not host:
        # Relative paths, "mailto:", plain text -- nothing to canonicalise.
        return candidate
    if host.startswith("www."):
        host = host[4:]

    # http and https address the same document; treat them as one.
    scheme = "https" if parts.scheme.lower() in ("", "http", "https") else parts.scheme.lower()

    netloc = host
    port = parts.port
    if port is not None and str(port) != _DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{port}"

    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_PARAMS
        and not key.lower().startswith(_TRACKING_PREFIXES)
    ]
    query = urlencode(sorted(kept), doseq=True)

    # Fragment is dropped entirely: it never selects a different document.
    return urlunsplit((scheme, netloc, path, query, ""))


def url_hash(url: str) -> str:
    """Stable SHA-256 of :func:`normalize_url`, hex-encoded (64 chars)."""
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()


def domain_of(url: str) -> str:
    """Registrable-ish host of *url* without ``www.``; empty string if unparsable."""
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host
