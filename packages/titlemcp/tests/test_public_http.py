from __future__ import annotations

import io
import unittest
from unittest import mock

from title_mcp.sources.public_http import (
    BODY_LIMIT,
    MAX_REDIRECTS,
    UnsafePublicDestination,
    _destination,
    _PinnedConnection,
    fetch_public_page,
)


class Response:
    def __init__(self, status=200, location=None, body=b"public page"):
        self.status = status
        self.location = location
        self.body = io.BytesIO(body)

    def getheader(self, name):
        return self.location if name == "Location" else "text/plain; charset=utf-8"

    def read1(self, count):
        return self.body.read(count)


class Connection:
    def __init__(self, response):
        self.response = response
        self.closed = False
        self.requests = []

    def request(self, *args, **kwargs):
        self.requests.append((args, kwargs))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class PublicFetchTests(unittest.TestCase):
    def test_private_and_alternate_addresses_are_rejected_before_connecting(self):
        cases = {
            "http://127.0.0.1/": ["127.0.0.1"],
            "http://127.1/": ["127.0.0.1"],
            "http://2130706433/": ["127.0.0.1"],
            "http://0x7f000001/": ["127.0.0.1"],
            "http://169.254.169.254/": ["169.254.169.254"],
            "http://10.1.2.3/": ["10.1.2.3"],
            "http://[::1]/": ["::1"],
            "http://[::ffff:127.0.0.1]/": ["::ffff:127.0.0.1"],
            "http://mixed.example/": ["8.8.8.8", "192.168.1.1"],
            "http://empty.example/": [],
        }
        for url, addresses in cases.items():
            factory = mock.Mock()
            with self.subTest(url=url), self.assertRaises(UnsafePublicDestination):
                fetch_public_page(
                    url,
                    timeout=1,
                    resolver=lambda *_, addresses=addresses: addresses,
                    connection_factory=factory,
                )
            factory.assert_not_called()

    def test_unsafe_url_forms_are_rejected_without_dns(self):
        resolver = mock.Mock(return_value=["8.8.8.8"])
        for url in (
            "file:///etc/passwd",
            "ftp://public.example/",
            "https://user:pass@public.example/",
            "https://public.example:8443/",
            "http://[fe80::1%25en0]/",
            "https://public.example\\@127.0.0.1/",
            "https://public.example/\r\nX-Test:x",
        ):
            with self.subTest(url=url), self.assertRaises(UnsafePublicDestination):
                _destination(url, resolver)
        resolver.assert_not_called()

    def test_every_redirect_is_revalidated_and_previous_connection_closed(self):
        for target in ("http://private.example/", "//private.example/", "file:///etc/passwd"):
            first = Connection(Response(302, target))
            factory = mock.Mock(return_value=first)

            def resolver(host, port):
                return ["127.0.0.1" if host == "private.example" else "8.8.8.8"]

            with self.subTest(target=target), self.assertRaises(UnsafePublicDestination):
                fetch_public_page(
                    "https://public.example/",
                    timeout=1,
                    resolver=resolver,
                    connection_factory=factory,
                )
            self.assertEqual(factory.call_count, 1)
            self.assertTrue(first.closed)

    def test_relative_public_redirect_and_public_html_remain_usable(self):
        connections = [Connection(Response(302, "/contact")), Connection(Response(body=b"HOA"))]
        destinations = []

        def factory(destination, timeout):
            destinations.append(destination)
            return connections[len(destinations) - 1]

        result = fetch_public_page(
            "https://public.example/start",
            timeout=2,
            resolver=lambda *_: ["8.8.8.8"],
            connection_factory=factory,
        )
        self.assertEqual(result.body, b"HOA")
        self.assertEqual(result.url, "https://public.example/contact")
        self.assertEqual([d.target for d in destinations], ["/start", "/contact"])
        self.assertTrue(all(c.closed for c in connections))
        self.assertNotIn("Authorization", connections[0].requests[0][1]["headers"])

    def test_connection_uses_verified_numeric_address_and_original_tls_hostname(self):
        destination = _destination("https://public.example/", lambda *_: ["8.8.8.8"])
        transport = mock.Mock()
        context = mock.Mock()
        with (
            mock.patch(
                "title_mcp.sources.public_http.socket.create_connection", return_value=transport
            ) as connect,
            mock.patch(
                "title_mcp.sources.public_http.ssl.create_default_context", return_value=context
            ),
        ):
            connection = _PinnedConnection(destination, 2)
            connection.connect()
        connect.assert_called_once_with(("8.8.8.8", 443), timeout=2)
        context.wrap_socket.assert_called_once_with(transport, server_hostname="public.example")

    def test_failed_tls_verification_closes_socket(self):
        destination = _destination("https://public.example/", lambda *_: ["8.8.8.8"])
        transport = mock.Mock()
        context = mock.Mock()
        context.wrap_socket.side_effect = OSError("certificate rejected")
        with (
            mock.patch(
                "title_mcp.sources.public_http.socket.create_connection", return_value=transport
            ),
            mock.patch(
                "title_mcp.sources.public_http.ssl.create_default_context", return_value=context
            ),
            self.assertRaises(OSError),
        ):
            _PinnedConnection(destination, 1).connect()
        transport.close.assert_called_once()

    def test_same_hostname_is_rechecked_on_redirect_after_dns_changes(self):
        resolver = mock.Mock(side_effect=[["8.8.8.8"], ["127.0.0.1"]])
        factory = mock.Mock(return_value=Connection(Response(302, "/next")))
        with self.assertRaises(UnsafePublicDestination):
            fetch_public_page(
                "https://public.example/",
                timeout=1,
                resolver=resolver,
                connection_factory=factory,
            )
        self.assertEqual(factory.call_count, 1)

    def test_redirects_and_body_size_are_bounded(self):
        factory = mock.Mock(side_effect=lambda *_: Connection(Response(302, "/again")))
        with self.assertRaises(UnsafePublicDestination):
            fetch_public_page(
                "https://public.example/",
                timeout=1,
                resolver=lambda *_: ["8.8.8.8"],
                connection_factory=factory,
            )
        self.assertEqual(factory.call_count, MAX_REDIRECTS + 1)
        connection = Connection(Response(body=b"x" * (BODY_LIMIT + 50)))
        response = fetch_public_page(
            "https://public.example/",
            timeout=1,
            resolver=lambda *_: ["8.8.8.8"],
            connection_factory=lambda *_: connection,
        )
        self.assertEqual(len(response.body), BODY_LIMIT + 1)
        self.assertTrue(connection.closed)
