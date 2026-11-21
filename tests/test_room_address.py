"""Unit tests: a listed or open project room's readable address (room_address.py), through the same
"make this room listed / private" path people use (pm_projects.set_visibility).
Run: .venv/bin/python -m unittest tests.test_room_address"""
import asyncio
import json
import unittest
import urllib.parse
from types import SimpleNamespace
from unittest import mock

from server import project_rooms
from server import room_address
from tests.test_projects import ANNA, PM, S, Base, project


def run(coro):
    return asyncio.run(coro)


def reply(status, body):
    async def read(content_type=None):
        return body
    return SimpleNamespace(status=status, json=read, release=lambda: None)


class Address(Base):
    def setUp(self):
        super().setUp()
        self.room = project("!a", "final project")
        self.lp.client.rooms = {"!a": self.room}
        self.members[("!a", ANNA)], self.mods["!a"] = "join", {ANNA}
        self.aliases, self.calls = {f"#lobby:{S}": "!l"}, []      # the server's directory: alias → room

        async def send(method, path, body=None, headers=None, timeout=None):
            self.calls.append((method, path))
            if "/directory/room/" in path:
                alias = urllib.parse.unquote(path.rsplit("/", 1)[1])
                if method == "GET":           # matrix_calls.room_of_alias
                    found = self.aliases.get(alias)
                    return reply(200, {"room_id": found}) if found else reply(404, {"errcode": "M_NOT_FOUND"})
                if method == "PUT":
                    if alias in self.aliases:
                        return SimpleNamespace(status=409)
                    self.aliases[alias] = json.loads(body)["room_id"]
                elif self.aliases.pop(alias, None) is None:
                    return SimpleNamespace(status=404)
            return SimpleNamespace(status=200)
        self.lp.client.send = send
        self.private = mock.patch("server.project_rooms.repo_is_private", mock.AsyncMock(return_value=False))
        self.private.start()
        self.addCleanup(self.private.stop)

    def canonical(self):
        return self.state["!a"].get("m.room.canonical_alias", ({}, PM))[0].get("alias")

    def test_listing_gives_a_readable_address_and_private_takes_it_away(self):
        """Screen test: a listed room showed its raw room ID in the room directory."""
        answer = run(self.lp.set_visibility(self.room, ANNA, "listed"))
        self.assertIn(f"Its address is #final-project:{S}.", answer)
        self.assertEqual((self.canonical(), self.aliases[f"#final-project:{S}"]), (f"#final-project:{S}", "!a"))
        run(self.lp.set_visibility(self.room, ANNA, "open"))                               # open: the same address
        self.assertEqual(sum(m == "PUT" and "/directory/room/" in p for m, p in self.calls), 1)
        self.assertTrue(run(self.lp.set_visibility(self.room, ANNA, "private")).startswith("This room is now private"))
        self.assertNotIn(f"#final-project:{S}", self.aliases)
        self.assertIsNone(self.canonical())
        self.assertEqual(self.state["!a"][room_address.ADDRESS_EVENT][0], {})

    def test_a_taken_name_is_numbered_and_lobby_is_never_imitated(self):
        self.aliases[f"#final-project:{S}"] = "!other"
        self.assertIn(f"#final-project-2:{S}", run(self.lp.set_visibility(self.room, ANNA, "listed")))
        self.assertEqual(room_address.localpart("Lobby"), "lobby-project")
        self.assertEqual(room_address.localpart("!!!"), "project")

    def test_a_private_repos_name_is_never_in_the_address(self):
        self.room.display_name = "Secret One tracker"
        self.bind("!a", "a/secret-one")
        with mock.patch("server.project_rooms.repo_is_private", mock.AsyncMock(return_value=True)):
            answer = run(self.lp.set_visibility(self.room, ANNA, "listed"))
        self.assertIn(f"#project:{S}", answer)
        self.assertNotIn("secret", json.dumps(self.aliases))

    def test_an_address_its_owners_chose_is_left_alone(self):
        self.state["!a"] = {"m.room.canonical_alias": ({"alias": f"#theirs:{S}"}, ANNA),
                            room_address.ADDRESS_EVENT: ({"alias": f"#theirs:{S}"}, ANNA)}   # a record not mine
        self.aliases[f"#theirs:{S}"] = "!a"
        run(self.lp.set_visibility(self.room, ANNA, "listed"))
        run(self.lp.set_visibility(self.room, ANNA, "private"))
        self.assertEqual(self.aliases[f"#theirs:{S}"], "!a")
        self.assertEqual(self.canonical(), f"#theirs:{S}")
        self.assertFalse(any(m == "DELETE" for m, _ in self.calls))

    def test_a_failure_is_said_and_never_undoes_the_setting(self):
        async def refuse(method, path, body=None, headers=None, timeout=None):
            return reply(403 if "/directory/room/" in path else 200, {})
        self.lp.client.send = refuse
        answer = run(self.lp.set_visibility(self.room, ANNA, "listed"))
        self.assertTrue(answer.startswith("This room is now listed"), answer)
        self.assertIn("shows by its ID", answer)
        self.state["!a"][room_address.ADDRESS_EVENT] = ({"alias": f"#final-project:{S}"}, PM)
        self.assertIn("set it to private again to retry", run(self.lp.set_visibility(self.room, ANNA, "private")))
        self.assertEqual(self.state["!a"]["m.room.join_rules"][0], {"join_rule": "invite"})   # closed all the same

    def test_a_record_that_outlived_its_address_never_removes_another_rooms(self):
        """GPT review: the alias was deleted and taken by another room since."""
        self.state["!a"] = {room_address.ADDRESS_EVENT: ({"alias": f"#final-project:{S}"}, PM)}
        self.aliases[f"#final-project:{S}"] = "!other"
        run(self.lp.set_visibility(self.room, ANNA, "private"))
        self.assertEqual(self.aliases[f"#final-project:{S}"], "!other")
        self.assertFalse(any(m == "DELETE" for m, _ in self.calls))
        self.assertEqual(self.state["!a"][room_address.ADDRESS_EVENT][0], {})                # my record goes

    def test_an_address_already_pointing_here_is_used_but_never_removed_and_others_are_kept(self):
        """GPT review: a PUT whose answer was lost (or the owners) left the alias pointing here; it is
        used, but not recorded as mine, so I never delete it. alt_aliases survive."""
        self.aliases[f"#final-project:{S}"] = "!a"
        alt = {"alt_aliases": [f"#old:{S}"]}
        self.state["!a"] = {"m.room.canonical_alias": (alt, ANNA)}
        self.assertIn(f"Its address is #final-project:{S}.", run(self.lp.set_visibility(self.room, ANNA, "listed")))
        self.assertEqual(self.state["!a"]["m.room.canonical_alias"][0], {**alt, "alias": f"#final-project:{S}"})
        self.assertNotIn(room_address.ADDRESS_EVENT, self.state["!a"])
        run(self.lp.set_visibility(self.room, ANNA, "private"))
        self.assertEqual(self.aliases[f"#final-project:{S}"], "!a")
        self.state["!a"].pop("m.room.canonical_alias")
        self.state["!a"][room_address.ADDRESS_EVENT] = ({"alias": f"#mine:{S}"}, PM)
        self.state["!a"]["m.room.canonical_alias"] = ({**alt, "alias": f"#mine:{S}"}, PM)
        self.aliases[f"#mine:{S}"] = "!a"
        run(self.lp.set_visibility(self.room, ANNA, "private"))
        self.assertEqual(self.state["!a"]["m.room.canonical_alias"][0], alt)               # mine goes, theirs stay

    def test_a_repo_named_address_that_cant_be_removed_stops_the_bind(self):
        """GPT re-check: a failed removal must not let the private repo be bound."""
        self.state["!a"] = {room_address.ADDRESS_EVENT: ({"alias": f"#secret-one:{S}"}, PM)}
        self.aliases[f"#secret-one:{S}"] = "!a"

        async def refuse_delete(method, path, body=None, headers=None, timeout=None):
            return reply(403, {}) if method == "DELETE" else await self.lp_send(method, path, body, headers, timeout)
        self.lp_send, self.lp.client.send = self.lp.client.send, refuse_delete
        with mock.patch("server.project_rooms.repo_is_private", mock.AsyncMock(return_value=True)):
            refused, _ = run(self.lp.close_for_private("!a", "a/secret-one"))
        self.assertIn("I haven't set the repo", refused)

    def test_binding_a_private_repo_replaces_an_address_that_names_it(self):
        """GPT review: listed (with its address) before the private repo was bound."""
        self.room.display_name = "secret-one"
        run(self.lp.set_visibility(self.room, ANNA, "listed"))
        self.assertEqual(self.canonical(), f"#secret-one:{S}")
        with mock.patch("server.project_rooms.repo_is_private", mock.AsyncMock(return_value=True)):
            self.assertEqual(run(self.lp.close_for_private("!a", "a/secret-one")), (None, ""))
        self.assertEqual(self.canonical(), f"#project:{S}")
        self.assertNotIn("secret", json.dumps(self.aliases))

    # At start (pm_projects.give_addresses): screen test, "final project !5nbmn…" in the directory, a
    # room listed before readable addresses existed. The same rules as a visibility change.
    def listed_before(self, room_id="!a", value=project_rooms.LISTED, **more):
        self.state.setdefault(room_id, {}).update({project_rooms.VISIBILITY_EVENT: ({"visibility": value}, PM), **more})

    def test_at_start_a_room_listed_before_addresses_gets_one_once(self):
        self.listed_before()
        run(self.lp.give_addresses())
        self.assertEqual((self.canonical(), self.aliases[f"#final-project:{S}"]), (f"#final-project:{S}", "!a"))
        run(self.lp.give_addresses())                       # the next start: nothing more
        self.assertEqual(sum(m == "PUT" and "/directory/room/" in p for m, p in self.calls), 1)

    def test_at_start_open_rooms_too_never_private_ones_the_lobby_or_owners_addresses(self):
        self.listed_before(value=project_rooms.OPEN)
        self.lp.client.rooms["!b"] = project("!b", "quiet room")                 # private: no address
        self.lp.client.rooms["!l"] = lobby = project("!l", "Lobby")
        lobby.canonical_alias = project_rooms.LOBBY_ALIAS
        self.listed_before("!l", project_rooms.OPEN)
        self.lp.client.rooms["!c"] = project("!c", "chosen")
        self.listed_before("!c", **{"m.room.canonical_alias": ({"alias": f"#ours:{S}"}, ANNA)})
        run(self.lp.give_addresses())
        self.assertEqual(self.canonical(), f"#final-project:{S}")
        self.assertEqual(sorted(self.aliases.values()), ["!a", "!l"])           # !l: only its own #lobby

    def test_at_start_one_rooms_failure_never_stops_the_others(self):
        """GPT review: a gh failure in one room escaped and could stop @pm's start."""
        self.lp.client.rooms = {"!z": project("!z", "broken"), "!a": self.room}
        self.listed_before()
        real = project_rooms.visibility

        async def visibility(client, room_id, strict=False):
            if room_id == "!z":
                raise OSError("connection reset")
            return await real(client, room_id, strict)
        with mock.patch("server.project_rooms.visibility", visibility), self.assertLogs("loop", "ERROR"):
            run(self.lp.give_addresses())
        self.assertEqual(self.canonical(), f"#final-project:{S}")

    def test_at_start_a_private_repos_name_never_shows(self):
        self.room.display_name = "Secret One tracker"
        self.bind("!a", "a/secret-one")
        self.listed_before()
        with mock.patch("server.project_rooms.repo_is_private", mock.AsyncMock(return_value=True)):
            run(self.lp.give_addresses())
        self.assertEqual(self.canonical(), f"#project:{S}")
        self.assertNotIn("secret", json.dumps(self.aliases))


if __name__ == "__main__":
    unittest.main()
