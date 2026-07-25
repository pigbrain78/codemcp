#!/usr/bin/env python3

import asyncio
import html.parser
import ipaddress
import logging
import socket
from urllib.parse import urljoin, urlparse

import httpx

from ..common import truncate_output_content
from ..mcp import mcp

__all__ = [
    "web_fetch",
]

MAX_REDIRECTS = 5
FETCH_TIMEOUT = 30.0
USER_AGENT = "codemcp-webfetch/1.0 (+https://github.com/ezyang/codemcp)"


class _TextExtractor(html.parser.HTMLParser):
    """Extracts visible text from HTML, skipping script/style content."""

    _SKIPPED_TAGS = frozenset({"script", "style", "noscript"})
    _BLOCK_TAGS = frozenset(
        {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}
    )

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self._skip_tag: str | None = None
        self.chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIPPED_TAGS:
            self._skip_depth += 1
            self._skip_tag = tag
        elif tag in self._BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self._skip_depth and tag == self._skip_tag:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = data.strip()
        if text:
            self.chunks.append(text + " ")


def html_to_text(html_content: str) -> str:
    """Convert HTML to plain, readable text.

    Args:
        html_content: The raw HTML to convert

    Returns:
        A whitespace-normalized plain-text rendering of the visible content
    """
    extractor = _TextExtractor()
    extractor.feed(html_content)
    text = "".join(extractor.chunks)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _validate_url(url: str) -> tuple[bool, str, str | None]:
    """Validate that a URL has an http(s) scheme and a host.

    Returns:
        A tuple of (is_valid, error_message, hostname)
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return (
            False,
            f"Only http:// and https:// URLs are supported, got: {url!r}",
            None,
        )
    if not parsed.hostname:
        return False, f"Invalid URL, missing host: {url!r}", None
    return True, "", parsed.hostname


def _resolve_is_public(hostname: str) -> bool:
    """Resolve hostname and check that every address is a public, routable address.

    This is a best-effort defense against SSRF (e.g. a page tricking the
    assistant into fetching cloud metadata endpoints or internal services) —
    it does not eliminate the risk, since the actual connection happens after
    this check, but it blocks the common cases.
    """
    try:
        infos = socket.getaddrinfo(hostname, None)
    except OSError:
        return False
    if not infos:
        return False
    for _family, _type, _proto, _canonname, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False
    return True


@mcp.tool()
async def web_fetch(url: str, chat_id: str | None = None) -> str:
    """Fetches a URL over HTTP(S) and returns its content as text, for pulling
    documentation, articles, or other web content into the conversation.

    HTML responses are converted to plain text (tags stripped, scripts/styles
    skipped). Other text content types (plain text, JSON, XML) are returned as-is.
    Non-text content types are not supported. Output is truncated if very large.

    For safety, only http:// and https:// URLs are supported, and the tool
    refuses to fetch hosts that resolve to a private, loopback, or otherwise
    non-public address (including across redirects) — it cannot be used to
    reach services on your local network or machine.

    Args:
        url: The URL to fetch (must be http:// or https://)
        chat_id: The unique ID of the current chat session

    Returns:
        The fetched content as text, or a description of the error
    """
    chat_id = "" if chat_id is None else chat_id

    current_url = url
    try:
        for _hop in range(MAX_REDIRECTS + 1):
            is_valid, error_message, hostname = _validate_url(current_url)
            if not is_valid or hostname is None:
                return f"Error fetching {url}: {error_message}"

            is_public = await asyncio.to_thread(_resolve_is_public, hostname)
            if not is_public:
                return (
                    f"Error fetching {url}: refusing to fetch {hostname!r} — it "
                    "resolves to a private, loopback, or otherwise non-public address."
                )

            async with httpx.AsyncClient(
                follow_redirects=False, timeout=FETCH_TIMEOUT
            ) as client:
                response = await client.get(
                    current_url, headers={"User-Agent": USER_AGENT}
                )

            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    return (
                        f"Error fetching {url}: received a redirect with no "
                        "Location header"
                    )
                current_url = urljoin(current_url, location)
                continue

            break
        else:
            return f"Error fetching {url}: too many redirects (max {MAX_REDIRECTS})"
    except httpx.HTTPError as e:
        return f"Error fetching {url}: {e}"
    except Exception as e:
        logging.error(f"Error in web_fetch: {e}", exc_info=True)
        return f"Error fetching {url}: {e}"

    if response.status_code >= 400:
        return f"Error fetching {url}: HTTP {response.status_code}"

    content_type = response.headers.get("content-type", "")
    if "html" in content_type:
        text = html_to_text(response.text)
    elif (
        not content_type
        or content_type.startswith("text/")
        or "json" in content_type
        or "xml" in content_type
    ):
        text = response.text
    else:
        return (
            f"Cannot display content of type {content_type!r} from {current_url} "
            f"as text (fetched {len(response.content)} bytes)."
        )

    text = truncate_output_content(text, prefer_end=False)

    redirect_note = f" (redirected from {url})" if current_url != url else ""
    return f"Fetched {current_url}{redirect_note} (HTTP {response.status_code})\n\n{text.strip()}"
