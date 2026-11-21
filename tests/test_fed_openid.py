"""Signing in to the side panel from another Matrix server (screen test: a matrix.org person in Element
Desktop got "couldn't verify you here"). Their token is checked with THEIR server, found as the spec
says, and only ever over HTTPS to public addresses. DNS and HTTP are stand-ins here: nothing leaves
this machine. Run: .venv/bin/python -m unittest tests.test_fed_openid"""
import asyncio
import json
import socket
import unittest
from types import SimpleNamespace
from unittest import mock

from server import console
from server import fed_dns
from server import fed_https
from server import fed_openid

TOKEN = "s3cr3t-openid-token"
ALICE = "@alice:matrix.org"
PUBLIC = {"matrix.org": "104.20.10.1", "matrix-federation.matrix.org": "104.20.10.2", "example.org": "93.184.216.34",
          "fed.example.org": "93.184.216.35", "evil.example": "45.33.32.156"}


class World:
    """DNS names → addresses, and what each (address, port, path) answers: (status, headers, chunks)."""
    def __init__(self, addresses=None, answers=None, srv=None):
        self.addresses = {**PUBLIC, **(addresses or {})}
        self.answers, self.srv, self.connected, self.requests = answers or {}, srv or {}, [], []

    def getaddrinfo(self, host, port, *a, **k):
        ips = self.addresses.get(host, [host] if host[0].isdigit() or ":" in host else None)
        if ips is None:
            raise socket.gaierror("no such name")
        ips = [ips] if isinstance(ips, str) else ips
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]

    def connect(self, tls_name, ip, port, timeout):
        world = self

        class Conn:
            sock = None

            def request(self, method, path, headers):
                self.path = path
                world.requests.append({"ip": ip, "port": port, "tls": tls_name, "path": path, "host": headers["Host"]})

            def getresponse(self):
                for (a_ip, a_port, prefix), (status, headers, chunks) in world.answers.items():
                    if (a_ip, a_port) == (ip, port) and self.path.startswith(prefix):
                        return Resp(status, headers, list(chunks))
                return Resp(404, {}, [b"{}"])

            def close(self):
                pass
        self.connected.append((ip, port))
        return Conn()

    def __enter__(self):
        self.patches = [mock.patch.object(socket, "getaddrinfo", self.getaddrinfo),
                        mock.patch.object(fed_https, "CONNECT", self.connect),
                        mock.patch.object(fed_dns, "lookup", lambda name, timeout=None: self.srv.get(name, []))]
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *a):
        for p in self.patches:
            p.stop()


class Resp:
    def __init__(self, status, headers, chunks):
        self.status, self.headers, self.chunks = status, headers, chunks

    def getheader(self, name):
        return self.headers.get(name)

    def read1(self, n):
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        return chunk() if callable(chunk) else chunk


def ok(body):
    return 200, {}, [json.dumps(body).encode()]


WELL_KNOWN = "/.well-known/matrix/server"
USERINFO = "/_matrix/federation/v1/openid/userinfo"


def check(server=ALICE.split(":")[1], token=TOKEN, client="203.0.113.9"):
    local = mock.Mock(return_value="@anna:bridge.localhost")
    return fed_openid.user_of(token, server, client, "bridge.localhost", local), local


class FederatedSignIn(unittest.TestCase):
    def setUp(self):
        fed_https._cache.clear()
        fed_openid._tries.clear()

    def test_a_matrix_org_person_is_verified_through_its_delegation(self):
        """matrix.org delegates to matrix-federation.matrix.org:443 in its well-known."""
        answers = {(PUBLIC["matrix.org"], 443, WELL_KNOWN): ok({"m.server": "matrix-federation.matrix.org:443"}),
                   (PUBLIC["matrix-federation.matrix.org"], 443, USERINFO): ok({"sub": ALICE})}
        with World(answers=answers) as world:
            self.assertEqual(check()[0], ALICE)
        asked = world.requests[-1]
        self.assertEqual((asked["tls"], asked["host"]), ("matrix-federation.matrix.org", "matrix-federation.matrix.org:443"))
        self.assertIn(f"access_token={TOKEN}", asked["path"])
        self.assertNotIn(TOKEN, world.requests[0]["path"])       # the well-known never sees the token

    def test_srv_records_are_used_and_tls_is_for_the_server_name(self):
        answers = {(PUBLIC["fed.example.org"], 8443, USERINFO): ok({"sub": "@bob:example.org"})}
        with World(answers=answers, srv={"_matrix-fed._tcp.example.org": [("fed.example.org", 8443)]}) as world:
            self.assertEqual(check("example.org")[0], "@bob:example.org")
        self.assertEqual((world.requests[-1]["tls"], world.requests[-1]["host"]), ("example.org", "example.org"))

    def test_no_well_known_and_no_srv_is_port_8448(self):
        answers = {(PUBLIC["example.org"], 8448, USERINFO): ok({"sub": "@bob:example.org"})}
        with World(answers=answers):
            self.assertEqual(check("example.org")[0], "@bob:example.org")

    def test_a_server_vouching_for_someone_elses_user_is_refused(self):
        for sub in ("@anna:bridge.localhost", "@alice:example.org", "@alice:matrix.org.evil", "alice", None, "@a\x01:matrix.org"):
            fed_https._cache.clear()
            answers = {(PUBLIC["matrix.org"], 8448, USERINFO): ok({"sub": sub})}
            with World(answers=answers):
                self.assertIsNone(check()[0], sub)

    def test_our_own_people_still_go_to_our_homeserver(self):
        with World() as world:
            user, local = check("bridge.localhost")
        self.assertEqual(user, "@anna:bridge.localhost")
        local.assert_called_once_with(TOKEN)
        self.assertEqual(world.connected, [])

    def test_a_server_on_a_private_address_is_never_contacted(self):
        for ip in ("10.0.0.5", "127.0.0.1", "169.254.169.254", "192.168.1.2", "::1", "fe80::1", "fd00::5",
                   "224.0.0.1", "0.0.0.0", "::ffff:127.0.0.1", "100.64.0.1"):
            fed_https._cache.clear()
            with World(addresses={"evil.example": ip}) as world:
                self.assertIsNone(check("evil.example")[0], ip)
            self.assertEqual(world.connected, [], ip)

    def test_a_name_with_one_private_address_among_public_ones_is_refused(self):
        with World(addresses={"evil.example": ["45.33.32.156", "10.0.0.1"]}) as world:
            self.assertIsNone(check("evil.example")[0])
        self.assertEqual(world.connected, [])

    def test_delegation_or_srv_to_a_private_address_is_refused(self):
        answers = {(PUBLIC["evil.example"], 443, WELL_KNOWN): ok({"m.server": "inside.example:8448"})}
        with World(addresses={"inside.example": "10.1.2.3"}, answers=answers) as world:
            self.assertIsNone(check("evil.example")[0])
        self.assertNotIn(("10.1.2.3", 8448), world.connected)
        fed_https._cache.clear()
        with World(addresses={"inside.example": "127.0.0.1"}, srv={"_matrix-fed._tcp.evil.example": [("inside.example", 8448)]}) as world:
            self.assertIsNone(check("evil.example")[0])
        self.assertEqual([c for c in world.connected if c[0] == "127.0.0.1"], [])

    def test_a_redirect_to_a_private_address_is_never_followed(self):
        answers = {(PUBLIC["evil.example"], 443, WELL_KNOWN): (302, {"Location": "https://inside.example/x"}, [b""]),
                   (PUBLIC["evil.example"], 8448, USERINFO): (302, {"Location": "https://inside.example/steal"}, [b""])}
        with World(addresses={"inside.example": "127.0.0.1"}, answers=answers) as world:
            self.assertIsNone(check("evil.example")[0])
        self.assertTrue(all(ip != "127.0.0.1" for ip, _ in world.connected), world.connected)
        self.assertEqual(sum(TOKEN in r["path"] for r in world.requests), 1)   # the token went to evil.example only

    def test_a_well_known_redirect_to_plain_http_is_not_followed(self):
        answers = {(PUBLIC["evil.example"], 443, WELL_KNOWN): (301, {"Location": "http://example.org/x"}, [b""])}
        with World(answers=answers) as world:
            check("evil.example")
        self.assertNotIn((PUBLIC["example.org"], 80), world.connected)

    def test_a_public_redirect_hop_is_followed_for_the_well_known_only(self):
        answers = {(PUBLIC["example.org"], 443, WELL_KNOWN): (301, {"Location": "https://fed.example.org/.well-known/matrix/server"}, [b""]),
                   (PUBLIC["fed.example.org"], 443, WELL_KNOWN): ok({"m.server": "fed.example.org:443"}),
                   (PUBLIC["fed.example.org"], 443, USERINFO): ok({"sub": "@bob:example.org"})}
        with World(answers=answers):
            self.assertEqual(check("example.org")[0], "@bob:example.org")

    def test_an_oversized_answer_is_refused(self):
        big = {(PUBLIC["matrix.org"], 8448, USERINFO): (200, {"Content-Length": "999999"}, [b"{}"])}
        drip = {(PUBLIC["matrix.org"], 8448, USERINFO): (200, {}, [b"x" * 4096] * 4)}   # no length: counted as read
        for answers in (big, drip):
            fed_https._cache.clear()
            with World(answers=answers):
                self.assertIsNone(check()[0])

    def test_a_slow_answer_is_refused_at_the_deadline(self):
        clock = [1000.0]

        def slow():
            clock[0] += fed_openid.DEADLINE_S + 1
            return b'{"sub": "@alice:matrix.org"}'
        answers = {(PUBLIC["matrix.org"], 8448, USERINFO): (200, {}, [slow])}
        with World(answers=answers), mock.patch.object(fed_https.time, "monotonic", lambda: clock[0]), \
                mock.patch.object(fed_openid.time, "monotonic", lambda: clock[0]):
            self.assertIsNone(check()[0])

    def test_malformed_server_names_are_refused_before_any_lookup(self):
        for name in ("", "matrix.org/evil", "matrix.org:99999", "matrix.org:0", "-bad.org", "a..b", "1.2.3", "ex ample.org",
                     "matrix.org:80:80", "[::1", "[nothost]", "user@matrix.org", "matrix.org?x", "matrix.org#", "x" * 300,
                     "mätrix.org", "matrix.org:+80", 42, ["matrix.org"]):
            with World() as world:
                self.assertIsNone(check(name)[0], name)
            self.assertEqual(world.connected, [], name)

    def test_tries_are_limited_per_client_and_per_server(self):
        with World() as world:
            for _ in range(fed_openid.PER_CLIENT):
                check("example.org", client="1.2.3.4")
            before = len(world.connected)
            self.assertIsNone(check("example.org", client="1.2.3.4")[0])
            self.assertEqual(len(world.connected), before)
            for n in range(fed_openid.PER_SERVER - fed_openid.PER_CLIENT):
                check("example.org", client=f"5.6.7.{n}")
            before = len(world.connected)
            check("example.org", client="9.9.9.9")
        self.assertEqual(len(world.connected), before)

    def test_discovery_is_cached_briefly(self):
        answers = {(PUBLIC["matrix.org"], 443, WELL_KNOWN): ok({"m.server": "matrix-federation.matrix.org:443"}),
                   (PUBLIC["matrix-federation.matrix.org"], 443, USERINFO): ok({"sub": ALICE})}
        with World(answers=answers) as world:
            check()
            check()
        self.assertEqual(sum(r["path"] == WELL_KNOWN for r in world.requests), 1)

    def test_the_token_is_never_logged(self):
        answers = {(PUBLIC["matrix.org"], 8448, USERINFO): ok({"sub": "@mallory:evil.example"})}
        with World(answers=answers), self.assertLogs("console", "INFO") as logs:
            check()
            check("evil.example")
        self.assertFalse(any(TOKEN in line for line in logs.output))


class SrvAnswers(unittest.TestCase):
    def test_an_srv_answer_is_read_and_a_damaged_one_is_none(self):
        q = fed_dns.question(7, "_matrix-fed._tcp.example.org")
        target = b"\x03fed\x07example\x03org\x00"
        rr = b"\xc0\x0c" + bytes([0, 33, 0, 1, 0, 0, 0, 60, 0, 6 + len(target)]) + bytes([0, 10, 0, 5, 0x20, 0xfb]) + target
        answer = bytearray(q + rr)
        answer[2:4], answer[6:8] = b"\x81\x80", b"\x00\x01"
        self.assertEqual(fed_dns.parse(bytes(answer), 7), [(10, 5, 8443, "fed.example.org")])
        with self.assertRaises(fed_dns.Malformed):
            fed_dns.parse(bytes(answer), 8)                        # someone else's answer
        with self.assertRaises(fed_dns.Malformed):
            fed_dns.parse(bytes(answer[:-5]), 7)                   # cut short
        loop = bytearray(answer)
        loop[len(q):len(q) + 2] = bytes([0xC0, len(q)])            # a pointer to itself
        with self.assertRaises(fed_dns.Malformed):
            fed_dns.parse(bytes(loop), 7)


class PanelRoute(unittest.TestCase):
    """The panel's POST /auth/openid, as Element's widget sends it, gives a session for that person."""
    def test_a_matrix_org_person_gets_a_session(self):
        from aiohttp.test_utils import TestClient, TestServer
        from aiohttp import web
        fed_https._cache.clear()
        fed_openid._tries.clear()
        answers = {(PUBLIC["matrix.org"], 443, WELL_KNOWN): ok({"m.server": "matrix-federation.matrix.org:443"}),
                   (PUBLIC["matrix-federation.matrix.org"], 443, USERINFO): ok({"sub": ALICE})}

        async def go():
            app = web.Application()
            app.add_routes([r for r in console.routes(SimpleNamespace(), lambda *a: "") if r.path == "/auth/openid"])
            async with TestClient(TestServer(app)) as client:
                headers = {"Host": "localhost:8650", "Origin": "http://localhost:8650"}
                body = {"access_token": TOKEN, "matrix_server_name": "matrix.org"}
                good = await (await client.post("/auth/openid", json=body, headers=headers)).json()
                bad = await (await client.post("/auth/openid", json={**body, "matrix_server_name": "evil.example"},
                                               headers=headers)).json()
            return good, bad
        with World(answers=answers):
            good, bad = asyncio.run(go())
        self.assertEqual(console.session_of(SimpleNamespace(headers={"Authorization": f"Bearer {good['session']}"})), (ALICE, None))
        self.assertEqual(bad, {"error": console.NOT_VERIFIED})


if __name__ == "__main__":
    unittest.main()
