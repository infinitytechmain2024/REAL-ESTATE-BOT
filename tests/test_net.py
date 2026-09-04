"""The SSRF guard.

These are the checks that stop a search result from pointing the fetcher at
the cloud metadata endpoint or at our own SearXNG on loopback.
"""

from __future__ import annotations

import pytest

from bot.utils import net
from bot.utils.net import UnsafeURLError, assert_safe_url

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8888/search",       # our own SearXNG
        "http://localhost/admin",             # resolves to loopback
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://10.0.0.5/internal",           # RFC1918
        "http://192.168.1.1/",                # RFC1918
        "http://172.16.0.1/",                 # RFC1918
        "http://[::1]/",                      # IPv6 loopback
        "http://[::ffff:127.0.0.1]/",         # IPv4-mapped loopback
        "http://0.0.0.0/",                    # unspecified
        "http://100.64.0.1/",                 # CGNAT
    ],
)
async def test_private_and_loopback_addresses_are_refused(url: str) -> None:
    with pytest.raises(UnsafeURLError):
        await assert_safe_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "gopher://example.com/",
        "data:text/html,<h1>hi</h1>",
    ],
)
async def test_non_http_schemes_are_refused(url: str) -> None:
    with pytest.raises(UnsafeURLError):
        await assert_safe_url(url)


async def test_embedded_credentials_are_refused() -> None:
    with pytest.raises(UnsafeURLError):
        await assert_safe_url("http://user:pass@93.184.216.34/")


async def test_public_literal_address_is_allowed() -> None:
    await assert_safe_url("https://93.184.216.34/page")  # no exception


async def test_hostname_resolving_to_a_private_address_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A public-looking name whose DNS answer is internal must still be blocked."""

    async def fake_getaddrinfo(host, port, **kwargs):  # type: ignore[no-untyped-def]
        return [(None, None, None, "", ("10.1.2.3", port))]

    _patch_resolver(monkeypatch, fake_getaddrinfo)
    with pytest.raises(UnsafeURLError, match="non-public"):
        await assert_safe_url("https://internal.example.com/")


async def test_a_single_private_answer_disqualifies_the_whole_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """We do not control which address the client picks, so one bad one is fatal."""

    async def fake_getaddrinfo(host, port, **kwargs):  # type: ignore[no-untyped-def]
        return [
            (None, None, None, "", ("93.184.216.34", port)),
            (None, None, None, "", ("127.0.0.1", port)),
        ]

    _patch_resolver(monkeypatch, fake_getaddrinfo)
    with pytest.raises(UnsafeURLError):
        await assert_safe_url("https://split.example.com/")


async def test_public_hostname_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_getaddrinfo(host, port, **kwargs):  # type: ignore[no-untyped-def]
        return [(None, None, None, "", ("93.184.216.34", port))]

    _patch_resolver(monkeypatch, fake_getaddrinfo)
    await assert_safe_url("https://example.com/listing/1")  # no exception


def _patch_resolver(monkeypatch: pytest.MonkeyPatch, fake) -> None:  # type: ignore[no-untyped-def]
    """Swap out the event loop's getaddrinfo for *fake*."""

    class _Loop:
        getaddrinfo = staticmethod(fake)

    monkeypatch.setattr(net.asyncio, "get_running_loop", lambda: _Loop())
