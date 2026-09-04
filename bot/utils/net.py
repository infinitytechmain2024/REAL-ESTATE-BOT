"""Network safety checks for URLs we did not choose ourselves.

The page fetcher downloads whatever a search engine hands it, and a search
engine will happily return a URL that resolves to something inside our own
network. Left unchecked that turns the bot into an SSRF proxy: a page that
redirects to ``http://169.254.169.254/`` (cloud metadata) or
``http://127.0.0.1:8888/`` (our own SearXNG) would be fetched with the
worker's credentials and its text handed to the LLM.

So every URL is screened before it is opened -- the original *and* every
redirect hop, because the interesting attacks all hide behind a redirect.
A host is allowed only when the scheme is http(s) and **every** address it
resolves to is a globally routable public address.

Known residual risk: DNS is resolved here and again by httpx when it opens
the connection, so a name that flips between a public and a private address
between the two lookups (DNS rebinding) can still slip through. Closing that
hole means dialling the validated IP directly and overriding SNI/Host, which
httpx does not expose cleanly; the check below stops every attack that does
not require control of an authoritative DNS server.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

from bot.logging_conf import get_logger

log = get_logger(__name__)

ALLOWED_SCHEMES = frozenset({"http", "https"})


class UnsafeURLError(Exception):
    """The URL points somewhere we refuse to fetch from."""


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether *ip* is a globally routable public address.

    ``is_global`` already excludes loopback (127/8, ::1), RFC1918 private
    ranges, link-local (169.254/16, fe80::/10 -- the cloud metadata endpoint
    lives here), CGNAT, multicast, reserved and unspecified addresses. IPv4
    addresses wrapped in IPv6 (``::ffff:127.0.0.1``) are unwrapped first so
    they cannot be used to smuggle a private address past the check.
    """
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped or (ip.sixtofour if ip.sixtofour else None)
        if mapped is not None:
            ip = mapped
    return ip.is_global


async def resolve_public_addresses(host: str, port: int) -> list[str]:
    """Resolve *host* and return its addresses, or raise :class:`UnsafeURLError`.

    A literal IP is checked directly without a DNS round trip. A name is
    resolved in the default executor -- ``getaddrinfo`` is blocking -- and
    rejected unless *every* address it returns is public, since we do not
    control which one the HTTP client will end up connecting to.
    """
    if not host:
        raise UnsafeURLError("URL has no host")

    # A literal address needs no lookup; strip the brackets IPv6 URLs carry.
    literal = host.strip("[]")
    try:
        ip = ipaddress.ip_address(literal)
    except ValueError:
        pass
    else:
        if not _is_public(ip):
            raise UnsafeURLError(f"address {literal} is not publicly routable")
        return [literal]

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"cannot resolve {host!r}: {exc.strerror or exc}") from exc
    except OSError as exc:
        raise UnsafeURLError(f"cannot resolve {host!r}: {exc}") from exc

    addresses = {info[4][0] for info in infos}
    if not addresses:
        raise UnsafeURLError(f"{host!r} resolved to nothing")

    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            raise UnsafeURLError(f"{host!r} resolved to an unparsable address") from None
        if not _is_public(ip):
            raise UnsafeURLError(f"{host!r} resolves to the non-public address {address}")

    return sorted(addresses)


async def assert_safe_url(url: str) -> None:
    """Raise :class:`UnsafeURLError` unless *url* is safe to fetch.

    Call this for the URL we were given and again for the target of every
    redirect -- checking only the first one is exactly the hole this closes.
    """
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise UnsafeURLError(f"malformed URL: {exc}") from exc

    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeURLError(f"scheme {scheme or '(none)'!r} is not allowed")

    if parts.username or parts.password:
        # Credentials in a URL are never something a search result legitimately
        # needs, and they are a common way to confuse host parsing.
        raise UnsafeURLError("URL carries embedded credentials")

    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise UnsafeURLError(f"invalid port: {exc}") from exc

    addresses = await resolve_public_addresses(parts.hostname or "", port)
    log.debug("net.url_allowed", host=parts.hostname, addresses=addresses)
