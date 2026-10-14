from __future__ import annotations

import ipaddress
import socket
import ssl
import time
from collections.abc import Callable
from http.client import HTTPConnection
from typing import Any, Literal
from urllib.parse import quote, urljoin, urlsplit

from pydantic import BaseModel, ConfigDict

BODY_LIMIT = 1_500_000
MAX_REDIRECTS = 5
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class UnsafePublicDestination(ValueError):
    """The selected destination is not an allowed public HTTP service."""


class PublicDestination(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    scheme: Literal["http", "https"]
    hostname: str
    address: str
    port: int
    target: str


class PublicPageResponse(BaseModel):
    url: str
    status: int
    content_type: str | None = None
    body: bytes


def _resolve(hostname: str, port: int) -> list[str]:
    return [entry[4][0] for entry in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)]


def _is_public(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if not address.is_global or address.is_multicast or address.is_reserved:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return _is_public(str(address.ipv4_mapped))
        if address.sixtofour is not None or address.teredo is not None:
            return False
    return True


def _destination(
    url: str,
    resolver: Callable[[str, int], list[str]],
) -> PublicDestination:
    if any(ord(character) < 33 or ord(character) == 127 for character in url) or "\\" in url:
        raise UnsafePublicDestination("Invalid URL characters.")
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        expected_port = {"http": 80, "https": 443}.get(parts.scheme.lower())
        if expected_port is None or not hostname or parts.username is not None:
            raise UnsafePublicDestination("Only public HTTP(S) destinations are allowed.")
        if "%" in hostname or parts.port not in (None, expected_port):
            raise UnsafePublicDestination("Unsupported host or port.")
        hostname = hostname.encode("idna").decode("ascii")
    except (ValueError, UnicodeError):
        raise UnsafePublicDestination("Invalid public URL.") from None
    addresses = list(dict.fromkeys(resolver(hostname, expected_port)))
    if not addresses or not all(_is_public(address) for address in addresses):
        raise UnsafePublicDestination("The destination must resolve entirely to public addresses.")
    target = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
    if parts.query:
        target += "?" + quote(parts.query, safe="=&?/%:@!$'()*+,;~-._")
    return PublicDestination(
        scheme=parts.scheme.lower(),
        hostname=hostname,
        address=addresses[0],
        port=expected_port,
        target=target,
    )


class _PinnedConnection(HTTPConnection):
    """Resolve once, connect to that numeric address, verify TLS for the hostname."""

    def __init__(self, destination: PublicDestination, timeout: float) -> None:
        super().__init__(destination.hostname, destination.port, timeout=timeout)
        self.destination = destination

    def connect(self) -> None:
        sock = socket.create_connection(
            (self.destination.address, self.destination.port), timeout=self.timeout
        )
        if self.destination.scheme == "https":
            try:
                sock = ssl.create_default_context().wrap_socket(
                    sock, server_hostname=self.destination.hostname
                )
            except BaseException:
                sock.close()
                raise
        self.sock = sock


def fetch_public_page(
    url: str,
    *,
    timeout: float,
    resolver: Callable[[str, int], list[str]] | None = None,
    connection_factory: Callable[[PublicDestination, float], Any] | None = None,
) -> PublicPageResponse:
    """Validate and pin every hop; never use ambient proxies, cookies or credentials."""
    resolver = resolver or _resolve
    connection_factory = connection_factory or _PinnedConnection
    deadline = time.monotonic() + timeout
    current = url
    for hop in range(MAX_REDIRECTS + 1):
        destination = _destination(current, resolver)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Page fetch timed out.")
        connection = connection_factory(destination, remaining)
        try:
            connection.request(
                "GET",
                destination.target,
                headers={
                    "User-Agent": "TitleMCP-HOA/1.0",
                    "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8",
                },
            )
            response = connection.getresponse()
            if response.status in REDIRECT_STATUSES:
                location = response.getheader("Location")
                if not location or hop == MAX_REDIRECTS:
                    raise UnsafePublicDestination("Invalid or excessive redirects.")
                current = urljoin(current, location)
                continue
            if response.status < 200 or response.status >= 300:
                raise ValueError("Public page request failed.")
            chunks: list[bytes] = []
            size = 0
            while size <= BODY_LIMIT:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Page fetch timed out.")
                chunk = response.read1(min(65536, BODY_LIMIT + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            return PublicPageResponse(
                url=current,
                status=response.status,
                content_type=response.getheader("Content-Type"),
                body=b"".join(chunks),
            )
        finally:
            connection.close()
    raise UnsafePublicDestination("Too many redirects.")
