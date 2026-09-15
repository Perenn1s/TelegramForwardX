"""Shared, protocol-aware proxy discovery for Telegram and Twitter."""

import logging
import socket
import urllib.request
from dataclasses import dataclass, replace
from urllib.parse import quote, unquote, urlsplit

log = logging.getLogger(__name__)
COMMON_PORTS = (7890, 7891, 7892, 7897, 1080, 10808, 10809)


@dataclass(frozen=True)
class Proxy:
    scheme: str
    host: str
    port: int
    username: str = ""
    password: str = ""

    @property
    def url(self):
        scheme = "socks5h" if self.scheme == "socks5" else self.scheme
        host = f"[{self.host}]" if ":" in self.host else self.host
        auth = ""
        if self.username:
            auth = f"{quote(self.username, safe='')}:{quote(self.password, safe='')}@"
        return f"{scheme}://{auth}{host}:{self.port}"

    @property
    def label(self):
        return f"{self.scheme}://{self.host}:{self.port}"

    def telegram(self):
        return dict(proxy_type=self.scheme, addr=self.host, port=self.port,
                    rdns=True, username=self.username or None,
                    password=self.password or None)


def parse_proxy(value):
    parsed = urlsplit(value if "://" in value else "http://" + value)
    scheme = {"socks": "socks5", "socks5h": "socks5"}.get(parsed.scheme, parsed.scheme)
    if scheme not in ("http", "socks5") or not parsed.hostname:
        raise ValueError("Proxy must use http:// or socks5:// with a host and port")
    port = parsed.port if parsed.port is not None else (8080 if scheme == "http" else 1080)
    if not 1 <= port <= 65535:
        raise ValueError("Proxy port must be between 1 and 65535")
    return Proxy(scheme, parsed.hostname, port,
                 unquote(parsed.username or ""), unquote(parsed.password or ""))


def system_candidates():
    sources = [urllib.request.getproxies()]
    # Stale environment proxies must not hide Windows' registry settings.
    registry = getattr(urllib.request, "getproxies_registry", None)
    if registry:
        sources.append(registry())
    for proxies in sources:
        for key in ("https", "http", "all", "socks"):
            value = proxies.get(key)
            if value:
                try:
                    yield parse_proxy(value)
                except ValueError:
                    log.warning("Ignoring unsupported %s system proxy; use HTTP or SOCKS5.", key)


def probe(proxy, timeout=1.0):
    """Check SOCKS negotiation or HTTP CONNECT, not just an open TCP port.

    No API credentials or messages are sent. HTTP CONNECT opens a tunnel to a
    Telegram DC, then closes it; SOCKS discovery only negotiates the protocol.
    """
    try:
        with socket.create_connection((proxy.host, proxy.port), timeout) as conn:
            conn.settimeout(timeout)
            with conn.makefile("rb") as stream:
                if proxy.scheme == "socks5":
                    conn.sendall(b"\x05\x02\x00\x02" if proxy.username else b"\x05\x01\x00")
                    reply = stream.read(2)
                    return reply == b"\x05\x00" or (bool(proxy.username) and reply == b"\x05\x02")
                auth = ""
                if proxy.username:
                    import base64
                    token = base64.b64encode(f"{proxy.username}:{proxy.password}".encode()).decode()
                    auth = f"Proxy-Authorization: Basic {token}\r\n"
                conn.sendall(("CONNECT 149.154.167.51:443 HTTP/1.1\r\n"
                              "Host: 149.154.167.51:443\r\n" + auth + "\r\n").encode())
                status = stream.readline(4096).split()
                return len(status) >= 2 and status[0].startswith(b"HTTP/1.") and status[1] == b"200"
    except (OSError, ValueError):
        return False


def resolve_proxy(settings):
    # Legacy explicit True/False settings remain supported.
    mode = settings.get("proxy_mode", "").strip().lower()
    if not mode:
        mode = "manual" if settings.getboolean("enable_proxy", fallback=False) else "off"
    if mode not in ("auto", "manual", "off"):
        raise ValueError("proxy_mode must be auto, manual, or off")
    if mode == "off":
        log.info("Proxy disabled; using direct/system TUN routing.")
        return None

    host = settings.get("proxy_host", "127.0.0.1").strip()
    port = settings.getint("proxy_port", fallback=7890)
    protocol = settings.get("proxy_type", "auto").strip().lower()
    if protocol not in ("auto", "http", "socks5") or not 1 <= port <= 65535:
        raise ValueError("Set proxy_type to auto/http/socks5 and proxy_port to 1..65535")
    explicit = settings.get("proxy_url", "").strip()
    configured = parse_proxy(explicit) if explicit else Proxy(
        "http" if protocol == "auto" else protocol, host, port)
    candidates = []
    if mode == "auto":
        candidates.extend((p, False) for p in system_candidates())
    candidates.append((configured, not explicit and protocol == "auto"))
    if mode == "auto":
        candidates.extend((Proxy("http", "127.0.0.1", p), True) for p in COMMON_PORTS)

    seen = set()
    for candidate, detect_protocol in candidates:
        variants = [candidate]
        if detect_protocol:
            variants.append(replace(candidate, scheme="socks5"))
        for proxy in variants:
            if proxy in seen:
                continue
            seen.add(proxy)
            if probe(proxy):
                log.info("Using proxy: %s", proxy.label)
                return proxy
    if mode == "manual":
        raise ConnectionError("Configured proxy failed protocol detection. Check Clash is running, "
                              "proxy_port/proxy_type and any proxy authentication.")
    log.warning("No usable system/local proxy found; using direct/system TUN routing. "
                "For a custom Clash port, set proxy_mode=manual and proxy_port in config.ini.")
    return None
