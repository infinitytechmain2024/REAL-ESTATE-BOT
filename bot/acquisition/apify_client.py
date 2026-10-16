"""The Apify REST API (``https://api.apify.com/v2``) for every adapter: Idealista listings and Facebook groups.

Safety rules shared by all callers:

* a **launch** (``POST acts/<actor>/runs``) is never repeated: if its answer is lost (network error, timeout, 5xx) or
  a 2xx body is unusable (invalid JSON, wrong schema or id, too large)
  the run may exist remotely, so ``launch`` raises ``LaunchUnknown`` and the caller records that instead of starting
  a second paid run; a 4xx (429 included) means nothing was started;
* reads (``GET``) are retried on 5xx and network errors (``retries`` times, backoff 2/4/8 s plus jitter) and on 429
  after ``Retry-After`` (its own limit); other 4xx are permanent;
* a response is bounded (``max_response_bytes``), and the token never appears in a repr, an error code or a log line.

``TemporaryApifyError``: try again later (the same run, never a new launch). ``PermanentApifyError``: this attempt
cannot succeed; the campaign goes on without it.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

API = "https://api.apify.com/v2/"
TERMINAL = frozenset({"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"})
ACTIVE = frozenset({"READY", "RUNNING", "TIMING-OUT", "ABORTING"})
_ID = re.compile(r"[A-Za-z0-9]+")
_ACTOR = re.compile(r"[\w-]+(?:[/~][\w-]+)?")


class ApifyError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class TemporaryApifyError(ApifyError):
    """Resume a known run later; never repeat an uncertain launch."""


class PermanentApifyError(ApifyError):
    """This source attempt cannot produce results; ordinary search continues."""


class LaunchUnknown(TemporaryApifyError):
    """The launch request got no usable answer: a run may exist remotely, so it must not be launched again."""


def valid_id(value: object) -> str:
    """A run/dataset id as Apify writes it, else ``PermanentApifyError``."""
    text = str(value or "").strip()
    if not _ID.fullmatch(text):
        raise PermanentApifyError("apify_id_schema")
    return text


def actor_path(actor_id: str) -> str:
    """``owner/name`` -> ``owner~name`` (the URL form); anything else is refused."""
    if not _ACTOR.fullmatch(actor_id or ""):
        raise ValueError("invalid actor id")
    return actor_id.replace("/", "~")


class ApifyHttp:
    def __init__(self, token: str, *, client: httpx.AsyncClient | None = None, timeout_seconds: float = 30,
                 retries: int = 3, backoff: tuple[float, ...] = (2, 4, 8), max_rate_limited: int = 5,
                 max_retry_after: float = 60, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 jitter: Callable[[], float] = lambda: random.uniform(0, 0.5),
                 max_response_bytes: int = 2_000_000) -> None:
        self._token = token
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False)
        self.timeout_seconds, self.retries, self.backoff = timeout_seconds, retries, backoff
        self.max_rate_limited, self.max_retry_after = max_rate_limited, max_retry_after
        self._sleep, self._jitter, self.max_response_bytes = sleep, jitter, max_response_bytes

    def __repr__(self) -> str:  # the token must not leak through a log line
        return "ApifyHttp(...)"

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _once(self, method: str, path: str, **kwargs: Any) -> tuple[int, dict[str, str], bytes]:
        async with self._client.stream(method, API + path, headers={"Authorization": f"Bearer {self._token}"},
                                       timeout=self.timeout_seconds, follow_redirects=False, **kwargs) as response:
            chunks: list[bytes] = []
            received = 0
            async for chunk in response.aiter_bytes():
                received += len(chunk)
                if received > self.max_response_bytes:
                    raise PermanentApifyError("apify_response_too_large")
                chunks.append(chunk)
            return response.status_code, {k.lower(): v for k, v in response.headers.items()}, b"".join(chunks)

    async def request(self, method: str, path: str, *, retry: bool = True, **kwargs: Any) -> Any:
        """The JSON body of one call; ``retry`` False: one attempt (a launch, or a caller with its own policy)."""
        failures = limited = 0
        while True:
            try:
                status, headers, body = await self._once(method, path, **kwargs)
            except httpx.HTTPError:
                if retry and failures < self.retries:
                    await self._sleep(self.backoff[min(failures, len(self.backoff) - 1)] + self._jitter())
                    failures += 1
                    continue
                raise TemporaryApifyError("apify_transport") from None
            if status == 429 and retry and limited < self.max_rate_limited:
                limited += 1
                await self._sleep(_retry_after(headers.get("retry-after"), self.max_retry_after) + self._jitter())
                continue
            if status >= 500 and retry and failures < self.retries:
                await self._sleep(self.backoff[min(failures, len(self.backoff) - 1)] + self._jitter())
                failures += 1
                continue
            if status >= 300:
                cls = TemporaryApifyError if status == 429 or status >= 500 else PermanentApifyError
                raise cls(f"apify_http_{status}")
            try:
                return json.loads(body)
            except ValueError:
                raise PermanentApifyError("apify_invalid_json") from None

    async def data(self, method: str, path: str, *, retry: bool = True, **kwargs: Any) -> dict[str, Any]:
        """``body["data"]`` of a run/actor call."""
        body = await self.request(method, path, retry=retry, **kwargs)
        if not isinstance(body, dict) or not isinstance(body.get("data"), dict):
            raise PermanentApifyError("apify_run_schema")
        return body["data"]

    async def launch(self, actor_id: str, actor_input: dict[str, Any], *, build: str = "", max_items: int,
                     max_charge_usd: float, timeout_seconds: float) -> dict[str, Any]:
        """Start one run (never retried). A lost answer -> ``LaunchUnknown``; a refusal -> permanent/temporary."""
        params: dict[str, Any] = {"maxTotalChargeUsd": max_charge_usd, "maxItems": max_items,
                                  "timeout": int(timeout_seconds), "restartOnError": "false"}
        if build:
            params["build"] = build
        try:
            run = await self.data("POST", f"acts/{actor_path(actor_id)}/runs", retry=False, params=params,
                                  json=actor_input)
        except ApifyError as exc:
            # Only an explicit 4xx/429 refusal proves nothing started; a lost answer or a broken 2xx body
            # (invalid JSON, wrong schema, too large) may hide a created run.
            if not exc.code.startswith("apify_http_") or exc.code.startswith("apify_http_5"):
                raise LaunchUnknown("apify_launch_unknown") from None
            raise
        try:
            valid_id(run.get("id"))
        except ApifyError:
            raise LaunchUnknown("apify_launch_unknown") from None
        return run

    async def run(self, run_id: str, *, wait_seconds: int = 0) -> dict[str, Any]:
        params = {"waitForFinish": wait_seconds} if wait_seconds else {}
        return await self.data("GET", f"actor-runs/{valid_id(run_id)}", params=params)

    async def abort(self, run_id: str) -> dict[str, Any]:
        return await self.data("POST", f"actor-runs/{valid_id(run_id)}/abort", retry=False)

    async def dataset_page(self, dataset_id: str, *, offset: int, limit: int) -> list[Any]:
        items = await self.request("GET", f"datasets/{valid_id(dataset_id)}/items",
                                   params={"format": "json", "clean": "true", "offset": offset, "limit": limit})
        if not isinstance(items, list):
            raise PermanentApifyError("apify_dataset_schema")
        return items


def _retry_after(value: str | None, cap: float) -> float:
    try:
        seconds = float(value) if value is not None else 1.0
    except ValueError:
        seconds = 1.0
    return max(0.0, min(cap, seconds))
