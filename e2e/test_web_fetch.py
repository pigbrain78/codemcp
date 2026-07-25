#!/usr/bin/env python3

"""Tests for the WebFetch subtool."""

from unittest import mock

import httpx

from codemcp.testing import MCPEndToEndTestCase
from codemcp.tools.web_fetch import html_to_text


class HtmlToTextTest(MCPEndToEndTestCase):
    """Test the pure html_to_text helper (no network involved)."""

    async def test_strips_tags_and_keeps_text(self):
        html = "<html><body><h1>Title</h1><p>Hello <b>world</b>.</p></body></html>"
        text = html_to_text(html)
        self.assertIn("Title", text)
        self.assertIn("Hello", text)
        self.assertIn("world", text)
        self.assertNotIn("<p>", text)
        self.assertNotIn("<b>", text)

    async def test_skips_script_and_style_content(self):
        html = (
            "<html><body><script>alert(1)</script>"
            "<style>body{color:red}</style><p>Visible</p></body></html>"
        )
        text = html_to_text(html)
        self.assertIn("Visible", text)
        self.assertNotIn("alert", text)
        self.assertNotIn("color:red", text)


class WebFetchTest(MCPEndToEndTestCase):
    """Test the WebFetch subtool."""

    async def test_web_fetch_html_page(self):
        html_body = (
            "<html><body><h1>Example</h1><p>Some content here.</p></body></html>"
        )
        response = httpx.Response(
            200, headers={"content-type": "text/html"}, text=html_body
        )

        with (
            mock.patch("codemcp.tools.web_fetch._resolve_is_public", return_value=True),
            mock.patch.object(
                httpx.AsyncClient, "get", new=mock.AsyncMock(return_value=response)
            ),
        ):
            async with self.create_client_session() as session:
                result_text = await self.call_tool_assert_success(
                    session,
                    "codemcp",
                    {"subtool": "WebFetch", "url": "https://example.com/page"},
                )

        self.assertIn("Fetched https://example.com/page", result_text)
        self.assertIn("Example", result_text)
        self.assertIn("Some content here.", result_text)

    async def test_web_fetch_plain_text(self):
        response = httpx.Response(
            200, headers={"content-type": "text/plain"}, text="Hello, World!"
        )

        with (
            mock.patch("codemcp.tools.web_fetch._resolve_is_public", return_value=True),
            mock.patch.object(
                httpx.AsyncClient, "get", new=mock.AsyncMock(return_value=response)
            ),
        ):
            async with self.create_client_session() as session:
                result_text = await self.call_tool_assert_success(
                    session,
                    "codemcp",
                    {"subtool": "WebFetch", "url": "https://example.com/raw.txt"},
                )

        self.assertIn("Hello, World!", result_text)

    async def test_web_fetch_rejects_non_http_scheme(self):
        async with self.create_client_session() as session:
            result_text = await self.call_tool_assert_success(
                session,
                "codemcp",
                {"subtool": "WebFetch", "url": "ftp://example.com/file"},
            )

        self.assertIn("Only http:// and https:// URLs", result_text)

    async def test_web_fetch_blocks_loopback_address(self):
        async with self.create_client_session() as session:
            result_text = await self.call_tool_assert_success(
                session,
                "codemcp",
                {"subtool": "WebFetch", "url": "http://127.0.0.1:8000/"},
            )

        self.assertIn("non-public address", result_text)

    async def test_web_fetch_blocks_link_local_metadata_address(self):
        async with self.create_client_session() as session:
            result_text = await self.call_tool_assert_success(
                session,
                "codemcp",
                {
                    "subtool": "WebFetch",
                    "url": "http://169.254.169.254/latest/meta-data/",
                },
            )

        self.assertIn("non-public address", result_text)

    async def test_web_fetch_reports_http_error_status(self):
        response = httpx.Response(
            404, headers={"content-type": "text/plain"}, text="not found"
        )

        with (
            mock.patch("codemcp.tools.web_fetch._resolve_is_public", return_value=True),
            mock.patch.object(
                httpx.AsyncClient, "get", new=mock.AsyncMock(return_value=response)
            ),
        ):
            async with self.create_client_session() as session:
                result_text = await self.call_tool_assert_success(
                    session,
                    "codemcp",
                    {"subtool": "WebFetch", "url": "https://example.com/missing"},
                )

        self.assertIn("HTTP 404", result_text)

    async def test_web_fetch_rejects_binary_content_type(self):
        response = httpx.Response(
            200,
            headers={"content-type": "application/pdf"},
            content=b"%PDF-1.4 fake pdf bytes",
        )

        with (
            mock.patch("codemcp.tools.web_fetch._resolve_is_public", return_value=True),
            mock.patch.object(
                httpx.AsyncClient, "get", new=mock.AsyncMock(return_value=response)
            ),
        ):
            async with self.create_client_session() as session:
                result_text = await self.call_tool_assert_success(
                    session,
                    "codemcp",
                    {"subtool": "WebFetch", "url": "https://example.com/file.pdf"},
                )

        self.assertIn("Cannot display content of type", result_text)

    async def test_web_fetch_follows_redirect(self):
        redirect_response = httpx.Response(
            302, headers={"location": "https://example.com/final"}
        )
        final_response = httpx.Response(
            200, headers={"content-type": "text/plain"}, text="final content"
        )
        responses = [redirect_response, final_response]

        async def fake_get(self_client, url, **kwargs):
            return responses.pop(0)

        with (
            mock.patch("codemcp.tools.web_fetch._resolve_is_public", return_value=True),
            mock.patch.object(httpx.AsyncClient, "get", new=fake_get),
        ):
            async with self.create_client_session() as session:
                result_text = await self.call_tool_assert_success(
                    session,
                    "codemcp",
                    {"subtool": "WebFetch", "url": "https://example.com/start"},
                )

        self.assertIn("final content", result_text)
        self.assertIn("redirected from https://example.com/start", result_text)

    async def test_web_fetch_too_many_redirects(self):
        async def fake_get(self_client, url, **kwargs):
            return httpx.Response(302, headers={"location": "https://example.com/next"})

        with (
            mock.patch("codemcp.tools.web_fetch._resolve_is_public", return_value=True),
            mock.patch.object(httpx.AsyncClient, "get", new=fake_get),
        ):
            async with self.create_client_session() as session:
                result_text = await self.call_tool_assert_success(
                    session,
                    "codemcp",
                    {"subtool": "WebFetch", "url": "https://example.com/start"},
                )

        self.assertIn("too many redirects", result_text)
