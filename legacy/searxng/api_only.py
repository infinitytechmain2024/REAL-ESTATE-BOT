# SPDX-License-Identifier: AGPL-3.0-or-later
"""JSON-API-only entry point for the vendored SearXNG.

The task this project was built for asks for "backend + JSON API only, no web
frontend". Deleting the frontend files from the vendored snapshot would make
upstream updates painful and risks breaking routes the API path also relies on,
so the UI is removed at the *routing* layer instead:

* ``searxng/settings/settings.yml`` sets ``search.formats: [json]``, so SearXNG
  will not render an HTML result page even if asked.
* this module wraps ``searx.webapp.app`` in a WSGI middleware that serves an
  explicit allowlist of paths and answers ``404`` for everything else --
  ``/``, ``/preferences``, ``/about``, ``/static/*``, ``/autocompleter``,
  ``/image_proxy``, ``/opensearch.xml`` and friends.

Serve it with::

    granian --interface wsgi --host 0.0.0.0 --port 8888 searxng.api_only:application

Every response also carries ``X-Robots-Tag: noindex, nofollow`` because this
instance is meant to be private to the bot.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable

# Paths the bot actually uses. Anything not listed here is not reachable.
ALLOWED_EXACT: frozenset[str] = frozenset(
    {
        "/search",  # the JSON search API
        "/healthz",  # container health check
        "/config",  # engine list / instance capabilities
        "/stats",  # engine timings, useful for debugging
        "/stats/errors",
        "/metrics",  # openmetrics, only if general.open_metrics is set
    }
)

_NOT_FOUND_BODY = b'{"error":"not found","detail":"this SearXNG instance only serves the JSON API"}\n'


class ApiOnlyMiddleware:
    """Reject any request outside :data:`ALLOWED_EXACT`."""

    def __init__(self, app: Callable[..., Iterable[bytes]]) -> None:
        self.app = app

    def __call__(self, environ: dict[str, object], start_response: Callable[..., object]) -> Iterable[bytes]:
        path = str(environ.get("PATH_INFO", "") or "/")
        # normalise a trailing slash so "/search/" is treated like "/search"
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/")
            # rewrite it for the wrapped app too, otherwise Flask 404s on the
            # very request we just allowed through
            environ["PATH_INFO"] = path

        if path not in ALLOWED_EXACT:
            start_response(
                "404 Not Found",
                [
                    ("Content-Type", "application/json; charset=utf-8"),
                    ("Content-Length", str(len(_NOT_FOUND_BODY))),
                    ("X-Robots-Tag", "noindex, nofollow"),
                ],
            )
            return [_NOT_FOUND_BODY]

        def _start_response(status: str, headers: list[tuple[str, str]], *args: object) -> object:
            headers = [*headers, ("X-Robots-Tag", "noindex, nofollow")]
            return start_response(status, headers, *args)

        return self.app(environ, _start_response)


def _build_application() -> Callable[..., Iterable[bytes]]:
    # Point SearXNG at our settings before importing the app: searx reads the
    # configuration at import time.
    os.environ.setdefault(
        "SEARXNG_SETTINGS_PATH",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings", "settings.yml"),
    )
    from searx.webapp import app  # pylint: disable=import-outside-toplevel

    return ApiOnlyMiddleware(app)


application = _build_application()
# granian/uwsgi conventionally look for `app` as well
app = application
