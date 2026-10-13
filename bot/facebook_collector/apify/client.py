"""Small async client for the Apify Actor and dataset APIs."""

from __future__ import annotations

import asyncio
import random
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any

import httpx


class ApifyClientError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class RunHandle:
    run_id: str | None
    dataset_id: str | None
    status: str


@dataclass(frozen=True, slots=True)
class RunStatus:
    run_id: str
    dataset_id: str | None
    status: str
    billed_usd: float | None = None
    status_message: str | None = None


class ApifyClient:
    """HTTP boundary with conservative retry behavior around paid Actor launches."""

    def __init__(
        self,
        token: str,
        *,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
        retry_count: int = 3,
        rate_retry_count: int = 3,
        timeout_seconds: float = 30,
    ) -> None:
        if retry_count < 0 or rate_retry_count < 0 or timeout_seconds <= 0:
            raise ValueError("invalid Apify client limits")
        self._token = token
        self._client = client or httpx.AsyncClient(
            base_url="https://api.apify.com/v2/", follow_redirects=False
        )
        self._sleep = sleep
        self._jitter = jitter
        self._retry_count = retry_count
        self._rate_retry_count = rate_retry_count
        self._timeout_seconds = timeout_seconds

    def __repr__(self) -> str:
        return "ApifyClient(...)"

    @property
    def configured(self) -> bool:
        return bool(self._token)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def run_actor(
        self,
        actor_id: str,
        build: str,
        actor_input: Mapping[str, Any],
        run_options: Mapping[str, Any] | None = None,
    ) -> RunHandle:
        actor = _identifier(actor_id, "actor_id", allow_separator=True).replace("/", "~")
        build_id = _identifier(build, "build")
        params = dict(run_options or {})
        params["build"] = build_id
        response = await self._launch_request(
            f"acts/{actor}/runs", params=params, json=dict(actor_input)
        )
        if response is None:
            return RunHandle(None, None, "LAUNCH_UNKNOWN")
        data = _data(response)
        run_id = _optional_identifier(data.get("id"))
        if run_id is None:
            raise ApifyClientError("apify_run_schema")
        return RunHandle(
            run_id,
            _optional_identifier(data.get("defaultDatasetId")),
            _text(data.get("status")) or "READY",
        )

    async def get_run_status(self, run_id: str) -> RunStatus:
        clean_run_id = _identifier(run_id, "run_id")
        response = await self._request("GET", f"actor-runs/{clean_run_id}")
        data = _data(response)
        returned_id = _optional_identifier(data.get("id"))
        status = _text(data.get("status"))
        if returned_id is None or status is None:
            raise ApifyClientError("apify_run_schema")
        usage = data.get("usageTotalUsd")
        return RunStatus(
            returned_id,
            _optional_identifier(data.get("defaultDatasetId")),
            status,
            float(usage) if isinstance(usage, (int, float)) and not isinstance(usage, bool) else None,
            _text(data.get("statusMessage")),
        )

    async def iter_dataset_items(
        self, dataset_id: str, *, offset: int = 0, page_size: int = 1000
    ) -> AsyncIterator[dict[str, Any]]:
        clean_dataset_id = _identifier(dataset_id, "dataset_id")
        if offset < 0 or not 1 <= page_size <= 1000:
            raise ValueError("invalid dataset page")
        next_offset = offset
        while True:
            response = await self._request(
                "GET",
                f"datasets/{clean_dataset_id}/items",
                params={"clean": "true", "format": "json", "offset": next_offset, "limit": page_size},
            )
            try:
                items = response.json()
            except ValueError:
                raise ApifyClientError("apify_invalid_json") from None
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise ApifyClientError("apify_dataset_schema")
            for item in items:
                yield item
            if len(items) < page_size:
                return
            next_offset += len(items)

    async def _launch_request(self, path: str, **kwargs: Any) -> httpx.Response | None:
        rate_attempt = 0
        while True:
            try:
                response = await self._client.request(
                    "POST", path, headers=self._headers(), timeout=self._timeout_seconds, **kwargs
                )
            except httpx.HTTPError:
                return None
            if response.status_code == 429 and rate_attempt < self._rate_retry_count:
                rate_attempt += 1
                await self._sleep(_retry_after(response))
                continue
            if response.status_code >= 500:
                return None
            if response.status_code >= 300:
                raise ApifyClientError(f"apify_http_{response.status_code}")
            return response

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        retry_attempt = 0
        rate_attempt = 0
        while True:
            try:
                response = await self._client.request(
                    method, path, headers=self._headers(), timeout=self._timeout_seconds, **kwargs
                )
            except httpx.HTTPError:
                if retry_attempt >= self._retry_count:
                    raise ApifyClientError("apify_transport") from None
                await self._backoff(retry_attempt)
                retry_attempt += 1
                continue
            if response.status_code == 429 and rate_attempt < self._rate_retry_count:
                rate_attempt += 1
                await self._sleep(_retry_after(response))
                continue
            if response.status_code >= 500 and retry_attempt < self._retry_count:
                await self._backoff(retry_attempt)
                retry_attempt += 1
                continue
            if response.status_code >= 300:
                raise ApifyClientError(f"apify_http_{response.status_code}")
            return response

    async def _backoff(self, retry_attempt: int) -> None:
        await self._sleep((2 ** (retry_attempt + 1)) + self._jitter())

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}


def _data(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        raise ApifyClientError("apify_invalid_json") from None
    if not isinstance(body, dict) or not isinstance(body.get("data"), dict):
        raise ApifyClientError("apify_run_schema")
    return body["data"]


def _identifier(value: str, field: str, *, allow_separator: bool = False) -> str:
    pattern = r"[A-Za-z0-9_-]+(?:[/~][A-Za-z0-9_-]+)?" if allow_separator else r"[A-Za-z0-9_-]+"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError(f"invalid {field}")
    return value


def _optional_identifier(value: Any) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]+", value) else None


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _retry_after(response: httpx.Response) -> float:
    value = response.headers.get("Retry-After", "")
    try:
        return max(0, min(300, float(value)))
    except ValueError:
        try:
            return max(0, min(300, parsedate_to_datetime(value).timestamp() - time.time()))
        except (TypeError, ValueError, OverflowError):
            return 2
