"""
Execute SQL via the Redash REST API (ad-hoc POST /api/query_results).

Polls ``/api/jobs/<id>`` until completion, then loads rows from
``GET /api/query_results/<id>``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import pandas as pd
from loguru import logger

from datasight.exceptions import ConnectionError, QueryError, QueryTimeoutError
from datasight.runner import DEFAULT_QUERY_TIMEOUT, _sql_preview

# Redash API job.status values (legacy numeric mapping used by the REST API).
_JOB_PENDING = 1
_JOB_STARTED = 2
_JOB_SUCCESS = 3
_JOB_FAILURE = 4
_JOB_CANCELLED = 5
_JOB_DEFERRED = 6
_JOB_SCHEDULED = 7

_JOB_WAIT_STATUSES = {_JOB_PENDING, _JOB_STARTED, _JOB_DEFERRED, _JOB_SCHEDULED}

_CLOSED_MSG = "RedashRunner is closed"


def _normalize_base_url(url: str) -> str:
    return url.rstrip("/")


def _query_result_to_dataframe(query_result: dict[str, Any]) -> pd.DataFrame:
    data = query_result.get("data") or {}
    cols_meta = data.get("columns") or []
    col_names = [
        str(c.get("name") or c.get("friendly_name") or f"col_{i}")
        for i, c in enumerate(cols_meta)
    ]
    rows = data.get("rows") or []
    if not rows:
        return pd.DataFrame(columns=col_names)
    return pd.DataFrame(rows)


def _job_query_result_id(job: dict[str, Any]) -> int | None:
    qrid = job.get("query_result_id")
    if qrid is None:
        qrid = job.get("result")
    if qrid is None:
        return None
    return int(qrid)


class RedashRunner:
    """Run SQL against a fixed Redash data source using the HTTP API."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        data_source_id: int,
        *,
        query_timeout: float = DEFAULT_QUERY_TIMEOUT,
        poll_interval: float = 0.5,
        http_timeout: float = 60.0,
        verify_ssl: bool = True,
        client: httpx.AsyncClient | None = None,
    ):
        self._owns_client = client is None
        self._base_url = _normalize_base_url(base_url)
        self._api_key = api_key
        self._data_source_id = int(data_source_id)
        self._query_timeout = float(query_timeout)
        self._poll_interval = float(poll_interval)
        self._headers = {"Authorization": f"Key {api_key}", "Content-Type": "application/json"}
        self._client: httpx.AsyncClient | None = None

        if client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers=self._headers,
                timeout=http_timeout,
                verify=verify_ssl,
            )
        else:
            self._client = client

        logger.info(
            f"Redash runner: base={self._base_url}, data_source_id={self._data_source_id}"
        )

    def close(self) -> None:
        """Release the HTTP client when no asyncio loop is running.

        When called under a running event loop, prefer ``await aclose()`` or
        ``async with RedashRunner(...)``.
        """
        if self._client is None or not self._owns_client:
            return
        client = self._client
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(client.aclose())
            self._client = None
        else:
            logger.warning(
                "RedashRunner.close() skipped closing the HTTP client while an event loop "
                "is running — use `async with` or `await runner.aclose()`"
            )

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None

    def __enter__(self) -> RedashRunner:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    async def __aenter__(self) -> RedashRunner:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.aclose()

    async def get_row_count(self, table: str) -> int | None:  # noqa: ARG002
        """Skip COUNT(*) via Redash during schema introspection."""
        return None

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise ConnectionError(_CLOSED_MSG)
        return self._client

    async def _post_query_results(self, sql: str) -> dict[str, Any]:
        client = self._require_client()
        payload = {"query": sql, "data_source_id": self._data_source_id, "max_age": 0}
        logger.debug(f"Redash ad-hoc query: {_sql_preview(sql)}")
        try:
            resp = await client.post("/api/query_results", json=payload)
        except httpx.RequestError as e:
            msg = f"Redash request failed: {e}"
            raise ConnectionError(msg) from e

        try:
            body = resp.json()
        except ValueError as e:
            msg = f"Redash returned non-JSON ({resp.status_code}): {resp.text[:500]}"
            raise QueryError(msg) from e

        if resp.status_code >= 400:
            err = ""
            if isinstance(body, dict):
                job = body.get("job") if isinstance(body.get("job"), dict) else {}
                err = str(job.get("error") or body.get("message") or resp.text)[:2000]
            msg = f"Redash HTTP {resp.status_code}: {err}"
            raise QueryError(msg)

        if not isinstance(body, dict):
            msg = "Redash returned unexpected JSON (expected object)"
            raise QueryError(msg)
        return body

    async def _fetch_job(self, job_id: str) -> dict[str, Any]:
        client = self._require_client()
        try:
            resp = await client.get(f"/api/jobs/{job_id}")
        except httpx.RequestError as e:
            msg = f"Redash job poll failed: {e}"
            raise ConnectionError(msg) from e

        try:
            body = resp.json()
        except ValueError as e:
            msg = f"Redash job poll non-JSON ({resp.status_code})"
            raise QueryError(msg) from e

        if resp.status_code >= 400:
            msg = f"Redash job HTTP {resp.status_code}: {body}"
            raise QueryError(msg)

        job = body.get("job") if isinstance(body, dict) else None
        if not isinstance(job, dict):
            msg = "Redash job response missing 'job' object"
            raise QueryError(msg)
        return job

    async def _poll_job(self, job_id: str) -> int:
        deadline = time.monotonic() + self._query_timeout
        while time.monotonic() < deadline:
            job = await self._fetch_job(job_id)
            status = int(job.get("status") or 0)
            if status in (_JOB_FAILURE, _JOB_CANCELLED):
                msg = str(job.get("error") or "Query execution failed")
                raise QueryError(msg)
            if status == _JOB_SUCCESS:
                qrid = _job_query_result_id(job)
                if qrid is None:
                    msg = "Redash job succeeded but returned no query_result_id"
                    raise QueryError(msg)
                return qrid

            if status not in _JOB_WAIT_STATUSES:
                msg = f"Unknown Redash job status: {status}"
                raise QueryError(msg)

            await asyncio.sleep(self._poll_interval)

        msg = (
            f"Redash query timed out after {self._query_timeout:.0f}s waiting for job {job_id}. "
            "Try a simpler query or increase REDASH_QUERY_TIMEOUT."
        )
        raise QueryTimeoutError(msg)

    async def _fetch_query_result(self, query_result_id: int) -> pd.DataFrame:
        client = self._require_client()
        try:
            resp = await client.get(f"/api/query_results/{query_result_id}")
        except httpx.RequestError as e:
            msg = f"Redash fetch result failed: {e}"
            raise ConnectionError(msg) from e

        try:
            body = resp.json()
        except ValueError as e:
            msg = f"Redash result non-JSON ({resp.status_code})"
            raise QueryError(msg) from e

        if resp.status_code >= 400:
            msg = f"Redash result HTTP {resp.status_code}: {body}"
            raise QueryError(msg)

        qr = body.get("query_result") if isinstance(body, dict) else None
        if not isinstance(qr, dict):
            msg = "Redash result JSON missing 'query_result'"
            raise QueryError(msg)
        return _query_result_to_dataframe(qr)

    async def run_sql(self, sql: str) -> pd.DataFrame:
        try:
            return await asyncio.wait_for(
                self._execute_once(sql),
                timeout=self._query_timeout,
            )
        except TimeoutError:
            msg = (
                f"Redash query timed out after {self._query_timeout:.0f}s. "
                "Try increasing REDASH_QUERY_TIMEOUT or simplifying the SQL."
            )
            raise QueryTimeoutError(msg) from None

    async def _execute_once(self, sql: str) -> pd.DataFrame:
        body = await self._post_query_results(sql)

        if "query_result" in body and isinstance(body["query_result"], dict):
            df = _query_result_to_dataframe(body["query_result"])
            logger.debug(f"Redash cache hit: {len(df)} rows")
            return df

        job_wrapped = body.get("job")
        if not isinstance(job_wrapped, dict):
            msg = "Redash response had neither query_result nor job"
            raise QueryError(msg)

        job_id = job_wrapped.get("id")
        if job_id is None:
            msg = "Redash job response missing id"
            raise QueryError(msg)

        status = int(job_wrapped.get("status") or 0)
        if status == _JOB_SUCCESS:
            qrid = _job_query_result_id(job_wrapped)
            if qrid is None:
                msg = "Redash job already succeeded but missing query_result_id"
                raise QueryError(msg)
            return await self._fetch_query_result(qrid)

        if status in (_JOB_FAILURE, _JOB_CANCELLED):
            msg = str(job_wrapped.get("error") or "Redash query failed")
            raise QueryError(msg)

        query_result_id = await self._poll_job(str(job_id))
        df = await self._fetch_query_result(query_result_id)
        logger.debug(f"Redash returned {len(df)} rows, {len(df.columns)} cols")
        return df
