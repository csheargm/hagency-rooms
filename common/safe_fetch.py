"""Fetch a public web page without any way to reach this machine or its network.

Each hop (the first request and every redirect) is resolved ONCE, every resolved address must be
public unicast, and the connection is made to that exact checked address (the hostname is kept
for the Host header, TLS SNI and certificate checks). No proxies are used, so nothing else
resolves the name. A hard overall deadline covers DNS, redirects and reading.
"""
import http.client
import ipaddress
import socket
import ssl
import time
import urllib.parse

MAX_BYTES = 1_000_000
MAX_REDIRECTS = 5
DEADLINE_S = 25
USER_AGENT = "Mozilla/5.0 (compatible; hagency-agent)"


class Refused(ValueError):
    pass


def public_ip(text: str) -> ipaddress._BaseAddress | None:
    ip = ipaddress.ip_address(text.split("%")[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped                   # ::ffff:127.0.0.1 is 127.0.0.1
    if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return None
    return ip


def resolve(host: str, port: int, deny: tuple = ()) -> str:
    """One public address for host, or Refused if ANY of its addresses isn't public (or is in `deny`,
    networks the caller also refuses: fed_https.py, this server's own addresses)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as e:
        raise Refused(f"can't resolve {host}") from e
    ips = [public_ip(info[4][0]) for info in infos]
    if not ips or any(ip is None or any(ip.version == net.version and ip in net for net in deny) for ip in ips):
        raise Refused("only public addresses (never this machine or its network)")
    return str(ips[0])


class _Pinned(http.client.HTTPSConnection):
    """HTTPS to a pre-checked IP, with SNI and certificate checks for the real hostname."""
    def __init__(self, host, ip, port, timeout):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _one_hop(url: str, deadline: float) -> tuple[int, str | None, bytes]:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise Refused("only http(s) addresses")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    ip = resolve(parts.hostname, port)
    timeout = max(1.0, deadline - time.monotonic())
    conn = (_Pinned(parts.hostname, ip, port, timeout) if parts.scheme == "https"
            else http.client.HTTPConnection(ip, port, timeout=timeout))
    path = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
    try:
        conn.request("GET", path, headers={"Host": parts.netloc, "User-Agent": USER_AGENT})
        resp = conn.getresponse()
        body = resp.read(MAX_BYTES) if resp.status < 300 or resp.status >= 400 else b""
        return resp.status, resp.getheader("Location"), body
    finally:
        conn.close()


def fetch(url: str) -> str:
    deadline = time.monotonic() + DEADLINE_S
    for _ in range(MAX_REDIRECTS + 1):
        if time.monotonic() > deadline:
            raise Refused("took too long")
        status, location, body = _one_hop(url, deadline)
        if 300 <= status < 400 and location:
            url = urllib.parse.urljoin(url, location)      # the next hop is checked the same way
            continue
        if status >= 400:
            raise Refused(f"the site answered {status}")
        return body.decode("utf-8", errors="replace")
    raise Refused("too many redirects")
