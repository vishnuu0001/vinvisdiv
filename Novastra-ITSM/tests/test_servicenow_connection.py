from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx

# Configuration requires a deployment secret at import time. Tests only need a
# non-empty value and never use it to issue or validate tokens.
os.environ.setdefault("JWT_SECRET", "test-only-secret")
os.environ.setdefault("ADMIN_SECRET", "test-only-admin-secret")

from backend.services.servicenow_sync import test_connection as check_connection


class _AsyncClientContext:
    def __init__(self, response: httpx.Response):
        self.client = AsyncMock()
        self.client.get.return_value = response
        self.client.post.return_value = httpx.Response(
            200,
            text="<html><title>ServiceNow login</title></html>",
            request=httpx.Request("POST", "https://example.service-now.com/login.do"),
        )

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, *_args):
        return False


class ServiceNowConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def _check_status(self, upstream_status: int) -> dict:
        request = httpx.Request(
            "GET", "https://example.service-now.com/api/now/table/incident"
        )
        response = httpx.Response(upstream_status, request=request)
        with (
            patch(
                "backend.services.servicenow_sync._auth_kwargs",
                new=AsyncMock(return_value=({"auth": ("user", "secret")}, {})),
            ),
            patch(
                "backend.services.servicenow_sync.httpx.AsyncClient",
                return_value=_AsyncClientContext(response),
            ),
        ):
            return await check_connection(
                base_url="https://example.service-now.com",
                username="user",
                password="secret",
                timeout_seconds=10,
                verify_ssl=True,
            )

    async def test_upstream_401_is_preserved(self):
        result = await self._check_status(401)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status_code"], 401)
        self.assertIn("authentication failed", result["message"].lower())

    async def test_upstream_403_is_preserved(self):
        result = await self._check_status(403)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status_code"], 403)
        self.assertIn("denied access", result["message"].lower())

    async def test_basic_401_falls_back_to_browser_session(self):
        table_request = httpx.Request(
            "GET", "https://example.service-now.com/api/now/table/incident"
        )
        context = _AsyncClientContext(httpx.Response(401, request=table_request))
        context.client.get.side_effect = [
            httpx.Response(401, request=table_request),
            httpx.Response(200, json={"result": [{"sys_id": "abc"}]}, request=table_request),
        ]
        context.client.post.return_value = httpx.Response(
            200,
            text="<script>var g_ck = 'session-user-token';</script>",
            request=httpx.Request("POST", "https://example.service-now.com/login.do"),
        )

        with (
            patch(
                "backend.services.servicenow_sync._auth_kwargs",
                new=AsyncMock(return_value=({"auth": ("user", "secret")}, {})),
            ),
            patch(
                "backend.services.servicenow_sync.httpx.AsyncClient",
                return_value=context,
            ),
        ):
            result = await check_connection(
                base_url="https://example.service-now.com",
                username="user",
                password="secret",
                timeout_seconds=10,
                verify_ssl=True,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["sample_records"], 1)
        retry_headers = context.client.get.await_args_list[1].kwargs["headers"]
        self.assertEqual(retry_headers["X-UserToken"], "session-user-token")
        self.assertNotIn("auth", context.client.get.await_args_list[1].kwargs)


if __name__ == "__main__":
    unittest.main()
