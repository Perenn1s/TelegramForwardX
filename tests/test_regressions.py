import asyncio
import configparser
import logging
import os
import socket
import tempfile
import threading
import unittest
from contextlib import contextmanager, suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import requests
from telethon.errors import FloodWaitError

from forwarder import failure_delay
from proxy_settings import Proxy, parse_proxy, probe, resolve_proxy, system_candidates
from runtime_logging import RepeatFilter
import load_Api_x


def settings(**values):
    config = configparser.ConfigParser()
    config.read_dict({"Settings": values})
    return config["Settings"]


@contextmanager
def local_server(reply):
    """A real loopback socket, with no external network or credentials."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    server.settimeout(2)
    received = []

    def serve():
        try:
            with server.accept()[0] as conn:
                conn.settimeout(2)
                received.append(conn.recv(4096))
                # Fragment replies to catch assumptions about TCP recv boundaries.
                conn.sendall(reply[:1])
                conn.sendall(reply[1:])
        except OSError:
            pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield server.getsockname()[1], received
    finally:
        thread.join(3)
        server.close()


class ProxyTests(unittest.TestCase):
    def test_http_connect(self):
        with local_server(b"HTTP/1.1 200 Connection established\r\n\r\n") as (port, received):
            self.assertTrue(probe(Proxy("http", "127.0.0.1", port)))
        self.assertTrue(received[0].startswith(b"CONNECT "))

    def test_socks_negotiation(self):
        with local_server(b"\x05\x00") as (port, received):
            self.assertTrue(probe(Proxy("socks5", "127.0.0.1", port)))
        self.assertEqual(received[0], b"\x05\x01\x00")

    def test_open_nonproxy_port_is_rejected(self):
        with local_server(b"SSH-2.0-test\r\n") as (port, _):
            self.assertFalse(probe(Proxy("http", "127.0.0.1", port)))

    def test_auth_required_without_credentials_is_rejected(self):
        with local_server(b"HTTP/1.1 407 Auth required\r\n\r\n") as (port, _):
            self.assertFalse(probe(Proxy("http", "127.0.0.1", port)))
        with local_server(b"\x05\x02") as (port, _):
            self.assertFalse(probe(Proxy("socks5", "127.0.0.1", port)))

    def test_parse_credentials_ipv6_and_remote_dns(self):
        proxy = parse_proxy("socks5h://a%40b:p%23ss@[::1]:7897")
        self.assertEqual(proxy.username, "a@b")
        self.assertEqual(proxy.password, "p#ss")
        self.assertEqual(proxy.url, "socks5h://a%40b:p%23ss@[::1]:7897")
        self.assertEqual(proxy.telegram()["proxy_type"], "socks5")
        self.assertTrue(proxy.telegram()["rdns"])
        self.assertNotIn("p#ss", proxy.label)

    def test_invalid_proxy_rejected(self):
        for value in ("http://localhost:0", "http://localhost:99999", "https://localhost:7890"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_proxy(value)

    @patch("proxy_settings.probe", return_value=True)
    @patch("proxy_settings.system_candidates", return_value=[Proxy("http", "localhost", 9999)])
    def test_auto_prefers_system_custom_port(self, _, check):
        result = resolve_proxy(settings(proxy_mode="auto"))
        self.assertEqual(result.port, 9999)
        self.assertEqual(result.scheme, "http")
        check.assert_called_once()

    @patch("proxy_settings.probe", side_effect=lambda p: p.scheme == "socks5")
    def test_manual_auto_detects_socks_on_custom_port(self, check):
        result = resolve_proxy(settings(proxy_mode="manual", proxy_port="23456"))
        self.assertEqual((result.scheme, result.port), ("socks5", 23456))
        self.assertEqual(check.call_count, 2)

    @patch("proxy_settings.probe", return_value=False)
    def test_manual_wrong_protocol_fails_without_scanning(self, check):
        with self.assertRaises(ConnectionError):
            resolve_proxy(settings(proxy_mode="manual", proxy_type="http"))
        check.assert_called_once()

    @patch("proxy_settings.probe", side_effect=lambda p: p.port == 7897 and p.scheme == "socks5")
    @patch("proxy_settings.system_candidates", return_value=[])
    def test_common_clash_port_fallback(self, _, check):
        result = resolve_proxy(settings(proxy_mode="auto"))
        self.assertEqual((result.port, result.scheme), (7897, "socks5"))
        probes = [call.args[0] for call in check.call_args_list]
        self.assertEqual(len(probes), len(set(probes)))

    @patch("proxy_settings.probe", return_value=False)
    @patch("proxy_settings.system_candidates", return_value=[])
    def test_auto_no_proxy_allows_tun(self, *_):
        self.assertIsNone(resolve_proxy(settings(proxy_mode="auto")))

    @patch("proxy_settings.probe")
    @patch("proxy_settings.system_candidates")
    def test_explicit_off_and_legacy_false_do_not_detect(self, discover, check):
        self.assertIsNone(resolve_proxy(settings(proxy_mode="off", enable_proxy="True")))
        self.assertIsNone(resolve_proxy(settings(enable_proxy="False")))
        discover.assert_not_called()
        check.assert_not_called()

    @patch("proxy_settings.probe", return_value=True)
    def test_legacy_true_is_manual(self, check):
        result = resolve_proxy(settings(enable_proxy="True", proxy_port="7891"))
        self.assertEqual(result.port, 7891)

    def test_registry_still_checked_with_stale_environment(self):
        with patch("urllib.request.getproxies", return_value={"http": "http://localhost:1"}), \
             patch("urllib.request.getproxies_registry", create=True,
                   return_value={"https": "http://localhost:7897"}):
            self.assertEqual([p.port for p in system_candidates()], [1, 7897])


class ErrorTests(unittest.TestCase):
    def test_repeated_logs_summarized(self):
        clock = Mock(return_value=0)
        filter_ = RepeatFilter(clock=clock)
        def record():
            return logging.LogRecord("telethon", logging.ERROR, "", 0, "Failure %s", ("detail",), None)
        self.assertTrue(filter_.filter(record()))
        for _ in range(100):
            self.assertFalse(filter_.filter(record()))
        clock.return_value = 30
        last = record()
        self.assertTrue(filter_.filter(last))
        self.assertIn("suppressed 100", last.getMessage())
        self.assertIn("detail", last.getMessage())

    def test_backoff_bounded(self):
        self.assertEqual([failure_delay(OSError(), i) for i in (1, 2, 3, 50)], [5, 10, 20, 300])

    def test_telegram_flood_wait_honored(self):
        self.assertEqual(failure_delay(FloodWaitError(None, capture=1234), 1), 1234)



if __name__ == "__main__":
    unittest.main()
