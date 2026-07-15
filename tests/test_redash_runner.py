"""Tests for Redash REST SQL runner (mocked HTTP)."""

from __future__ import annotations

import json

import httpx
import pandas as pd
import pytest

from datasight.exceptions import QueryError, QueryTimeoutError
from datasight.redash_runner import RedashRunner


def _json_response(obj: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=obj)


@pytest.mark.asyncio
async def test_redash_immediate_query_result():
    """POST returns cached query_result without job polling."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/query_results":
            body = {
                "query_result": {
                    "data": {
                        "columns": [{"name": "ok"}],
                        "rows": [{"ok": 1}],
                    }
                }
            }
            return _json_response(body)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://redash.test") as http_client:
        runner = RedashRunner(
            base_url="http://redash.test",
            api_key="k",
            data_source_id=9,
            query_timeout=5.0,
            poll_interval=0.01,
            client=http_client,
        )
        try:
            df = await runner.run_sql("SELECT 1 AS ok")
        finally:
            await runner.aclose()

    assert list(df.columns) == ["ok"]
    assert df.iloc[0]["ok"] == 1


@pytest.mark.asyncio
async def test_redash_job_poll_then_fetch():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/query_results":
            return _json_response({"job": {"id": "job-1", "status": 1}})
        if request.method == "GET" and request.url.path == "/api/jobs/job-1":
            calls["n"] += 1
            if calls["n"] < 2:
                return _json_response({"job": {"id": "job-1", "status": 2}})
            return _json_response(
                {"job": {"id": "job-1", "status": 3, "query_result_id": 42, "result": 42}}
            )
        if request.method == "GET" and request.url.path == "/api/query_results/42":
            return _json_response(
                {
                    "query_result": {
                        "data": {
                            "columns": [{"name": "x"}],
                            "rows": [{"x": "a"}, {"x": "b"}],
                        }
                    }
                }
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://redash.test") as http_client:
        runner = RedashRunner(
            base_url="http://redash.test",
            api_key="k",
            data_source_id=1,
            query_timeout=10.0,
            poll_interval=0.01,
            client=http_client,
        )
        try:
            df = await runner.run_sql("SELECT x FROM t")
        finally:
            await runner.aclose()

    assert isinstance(df, pd.DataFrame)
    assert len(df) == 2


@pytest.mark.asyncio
async def test_redash_job_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/query_results":
            return _json_response({"job": {"id": "j", "status": 4, "error": "boom"}})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://r.test") as http_client:
        runner = RedashRunner(
            base_url="http://r.test",
            api_key="k",
            data_source_id=1,
            query_timeout=5.0,
            client=http_client,
        )
        try:
            with pytest.raises(QueryError, match="boom"):
                await runner.run_sql("SELECT bad")
        finally:
            await runner.aclose()


@pytest.mark.asyncio
async def test_redash_poll_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/query_results":
            return _json_response({"job": {"id": "slow", "status": 1}})
        if request.method == "GET" and request.url.path == "/api/jobs/slow":
            return _json_response({"job": {"id": "slow", "status": 1}})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://r.test") as http_client:
        runner = RedashRunner(
            base_url="http://r.test",
            api_key="k",
            data_source_id=1,
            query_timeout=0.15,
            poll_interval=0.05,
            client=http_client,
        )
        try:
            with pytest.raises(QueryTimeoutError):
                await runner.run_sql("SELECT 1")
        finally:
            await runner.aclose()


@pytest.mark.asyncio
async def test_redash_post_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "denied"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://r.test") as http_client:
        runner = RedashRunner(
            base_url="http://r.test",
            api_key="k",
            data_source_id=1,
            client=http_client,
        )
        try:
            with pytest.raises(QueryError, match="403"):
                await runner.run_sql("SELECT 1")
        finally:
            await runner.aclose()


@pytest.mark.asyncio
async def test_redash_post_payload_includes_data_source():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/query_results":
            captured["body"] = json.loads(request.content.decode())
            return _json_response(
                {"query_result": {"data": {"columns": [{"name": "n"}], "rows": [{"n": 0}]}}}
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://r.test") as http_client:
        runner = RedashRunner(
            base_url="http://r.test",
            api_key="secret",
            data_source_id=7,
            client=http_client,
        )
        try:
            await runner.run_sql("SELECT 0 AS n")
        finally:
            await runner.aclose()

    assert captured["body"]["data_source_id"] == 7
    assert captured["body"]["max_age"] == 0
    assert "SELECT 0" in captured["body"]["query"]


@pytest.mark.asyncio
async def test_redash_get_row_count_returns_none():
    transport = httpx.MockTransport(lambda r: httpx.Response(404))
    async with httpx.AsyncClient(transport=transport, base_url="http://r.test") as http_client:
        runner = RedashRunner(
            base_url="http://r.test",
            api_key="k",
            data_source_id=1,
            client=http_client,
        )
        try:
            assert await runner.get_row_count("any") is None
        finally:
            await runner.aclose()
