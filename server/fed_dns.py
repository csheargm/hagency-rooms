"""SRV records with the standard library only, for Matrix server discovery (fed_https.py): which host
and port another homeserver's federation API is on (_matrix-fed._tcp.<name>, then _matrix._tcp).

One UDP question to this machine's resolver (/etc/resolv.conf), a random query ID, the answer taken
only from the server asked and only if it echoes that ID. Any problem (no resolver, a timeout, a
truncated, malformed or failed answer) is "no SRV record": discovery then falls back to port 8448,
which is what the spec does for a name without one. Whatever host a record names is checked again
by the caller like any other (public addresses only: safe_fetch.resolve).
"""
import ipaddress
import secrets
import socket
import struct
import time

RESOLV_CONF = "/etc/resolv.conf"
TIMEOUT_S = 2.0
MAX_ANSWER = 4096
MAX_RECORDS = 20
MAX_POINTERS = 20                       # name-compression jumps per name: no loops
MAX_NAME = 253
TYPE_SRV, CLASS_IN = 33, 1
NO_SUCH_NAME = 3


class Malformed(ValueError):
    """The resolver's answer can't be read: treated as no record."""


def nameservers(path: str = RESOLV_CONF) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read(65536).splitlines()
    except OSError:
        return []                             # no resolver configured: no SRV records (port 8448)
    found = []
    for line in lines:
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "nameserver":
            try:
                found.append(str(ipaddress.ip_address(parts[1].split("%")[0])))
            except ValueError:
                continue                      # a damaged line: the others still count
    return found[:3]


def question(qid: int, name: str) -> bytes:
    labels = b"".join(bytes([len(p)]) + p.encode("ascii") for p in name.rstrip(".").split("."))
    return struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0) + labels + b"\0" + struct.pack(">HH", TYPE_SRV, CLASS_IN)


def read_name(data: bytes, pos: int) -> tuple[str, int]:
    """A (possibly compressed) domain name at `pos`; returns it and the position after it."""
    labels, jumps, end = [], 0, None
    while True:
        if pos >= len(data):
            raise Malformed("name runs past the answer")
        size = data[pos]
        if size & 0xC0 == 0xC0:               # a pointer to an earlier name
            if pos + 1 >= len(data) or jumps >= MAX_POINTERS:
                raise Malformed("bad pointer")
            end = pos + 2 if end is None else end
            pos, jumps = ((size & 0x3F) << 8) | data[pos + 1], jumps + 1
            continue
        if size & 0xC0:
            raise Malformed("unknown label type")
        if size == 0:
            return ".".join(labels), (pos + 1 if end is None else end)
        label = data[pos + 1:pos + 1 + size].decode("ascii", errors="replace")
        if "." in label or not label.isprintable():
            raise Malformed("a label no host name has")   # "a.b" as one label would read as two
        labels.append(label)
        pos += 1 + size


def parse(data: bytes, qid: int, name: str = "") -> list[tuple[int, int, int, str]]:
    """(priority, weight, port, target) of each SRV answer to OUR question about `name` (the echoed
    question and each record's owner must be that name; GPT review); [] for "no such name"."""
    want = name.rstrip(".").lower()
    if len(data) < 12:
        raise Malformed("too short")
    rid, flags, qd, an = struct.unpack(">HHHH", data[:8])
    if rid != qid or not flags & 0x8000 or flags & 0x0200:   # not our answer, not an answer, or truncated
        raise Malformed("not the answer to our question")
    if qd != 1:
        raise Malformed("not one question")
    asked, pos = read_name(data, 12)
    if want and asked.lower() != want or data[pos:pos + 4] != struct.pack(">HH", TYPE_SRV, CLASS_IN):
        raise Malformed("an answer to another question")   # checked before "no such name" too (GPT re-check)
    if flags & 0xF == NO_SUCH_NAME:
        return []
    if flags & 0xF:
        raise Malformed("the resolver failed")
    pos += 4
    found = []
    for _ in range(min(an, MAX_RECORDS)):
        owner, pos = read_name(data, pos)
        if pos + 10 > len(data):
            raise Malformed("record header runs past the answer")
        rtype, rclass, _, size = struct.unpack(">HHIH", data[pos:pos + 10])
        pos += 10
        if pos + size > len(data):
            raise Malformed("record runs past the answer")
        if rtype == TYPE_SRV and rclass == CLASS_IN and size >= 7 and (not want or owner.lower() == want):
            prio, weight, port = struct.unpack(">HHH", data[pos:pos + 6])
            target, end = read_name(data[:pos + size], pos + 6)   # the target ends inside its record
            if end == pos + size:
                found.append((prio, weight, port, target))
        pos += size
    return found


def ask(server: str, name: str, timeout: float) -> list[tuple[int, int, int, str]]:
    qid = secrets.randbelow(65536)
    family = socket.AF_INET6 if ":" in server else socket.AF_INET
    with socket.socket(family, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.connect((server, 53))            # a connected socket only takes datagrams from that server
        sock.send(question(qid, name))
        return parse(sock.recv(MAX_ANSWER), qid, name)


def lookup(name: str, deadline: float | None = None) -> list[tuple[str, int]]:
    """(host, port) of each usable SRV record for `name`, best first; [] when there is none (or the
    name is too long to ask about). All resolvers together stop at `deadline` (time.monotonic)."""
    if len(name.rstrip(".")) > MAX_NAME or any(not 0 < len(p) < 64 for p in name.rstrip(".").split(".")):
        return []
    deadline = time.monotonic() + TIMEOUT_S if deadline is None else deadline
    for server in nameservers():
        left = min(TIMEOUT_S, deadline - time.monotonic())
        if left <= 0:
            return []
        try:
            records = ask(server, name, left)
        except (OSError, Malformed, UnicodeError):
            continue                          # this resolver didn't answer usefully: the next one, else none
        usable = [r for r in records if r[3] and r[3] != "." and 0 < r[2] < 65536]   # "." : no service there
        return [(r[3], r[2]) for r in sorted(usable, key=lambda r: (r[0], -r[1]))]
    return []
