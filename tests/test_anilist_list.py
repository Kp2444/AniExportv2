"""Reading an AniList list back out.

Read-only, so this is the safest path in the tool: no token is needed for a
public list, and nothing here can write to an account.
"""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import generate_mal_xml as gen
import gui
from tests import serve


def media(**over):
    base = {"id": 30642, "idMal": 642, "format": "MANGA", "status": "FINISHED",
            "popularity": 1000, "title": {"english": "Vinland Saga", "romaji": "Vinland Saga"},
            "episodes": None, "chapters": 224, "volumes": 29,
            "startDate": {"year": 2005}, "coverImage": {"medium": "u"}}
    base.update(over)
    return base


def entry(**over):
    base = {"id": 1, "status": "COMPLETED", "progress": 224, "progressVolumes": 29,
            "repeat": 0, "notes": "a note", "score": 80,
            "startedAt": {"year": 2024, "month": 3, "day": 1},
            "completedAt": {"year": 2025, "month": 6, "day": 7},
            "media": media()}
    base.update(over)
    return base


def collection(lists, has_next=False):
    return {"MediaListCollection": {"hasNextChunk": has_next, "lists": lists}}


def group(entries, custom=False, status="COMPLETED"):
    return {"name": "Completed", "status": status, "isCustomList": custom, "entries": entries}


class ReverseStatusTests(unittest.TestCase):
    def test_every_anilist_status_maps_back_for_both_media_types(self):
        # Taken from the enum itself, so a future AniList addition fails here
        # rather than silently producing None on an import.
        enum = {"CURRENT", "PLANNING", "COMPLETED", "DROPPED", "PAUSED", "REPEATING"}
        for media_type in ("anime", "manga"):
            with self.subTest(media_type=media_type):
                self.assertEqual(set(gen.ANILIST_STATUS_REVERSE[media_type]), enum)
                for label in gen.ANILIST_STATUS_REVERSE[media_type].values():
                    self.assertTrue(label)

    def test_the_two_maps_agree_in_the_directions_that_are_invertible(self):
        # ANILIST_STATUS_MAP is many-to-one, so only check the round trip that
        # a single media type actually defines.
        for media_type, forward in (("anime", "watching"), ("manga", "reading")):
            with self.subTest(media_type=media_type):
                enum_value = gen.ANILIST_STATUS_MAP[forward]
                back = gen.ANILIST_STATUS_REVERSE[media_type][enum_value]
                self.assertEqual(gen.ANILIST_STATUS_MAP[back.casefold()], enum_value)

    def test_a_rewatch_and_a_reread_are_not_confused(self):
        self.assertEqual(gen.ANILIST_STATUS_REVERSE["anime"]["REPEATING"], "Rewatching")
        self.assertEqual(gen.ANILIST_STATUS_REVERSE["manga"]["REPEATING"], "Rereading")
        self.assertEqual(gen.ANILIST_STATUS_REVERSE["anime"]["CURRENT"], "Watching")
        self.assertEqual(gen.ANILIST_STATUS_REVERSE["manga"]["CURRENT"], "Reading")


class RowMappingTests(unittest.TestCase):
    def test_a_manga_entry_becomes_a_row_the_builders_understand(self):
        row = gen._row_from_list_entry(entry(), "manga")
        self.assertEqual(row["id"], 642)
        self.assertEqual(row["anilist_id"], 30642)
        self.assertEqual(row["status"], "Completed")
        self.assertEqual(row["score"], 8, "POINT_100 is divided back to the 0-10 rows use")
        self.assertEqual(row["my_read_chapters"], 224)
        self.assertEqual(row["my_read_volumes"], 29)
        self.assertEqual(row["comments"], "a note")
        self.assertEqual(row["start_date"], "2024-03-01")
        self.assertEqual(row["finish_date"], "2025-06-07")
        self.assertTrue(row["can_export_xml"])

    def test_an_anime_entry_uses_episodes_and_never_volumes(self):
        node = media(id=339, idMal=339, format="TV", episodes=13, chapters=None, volumes=None,
                     title={"english": "Serial Experiments Lain", "romaji": "Lain"})
        row = gen._row_from_list_entry(entry(media=node, status="CURRENT", progress=3), "anime")
        self.assertEqual(row["my_watched_episodes"], 3)
        self.assertNotIn("my_read_chapters", row)
        self.assertNotIn("my_read_volumes", row)
        self.assertEqual(row["status"], "Watching")

    def test_a_partial_date_becomes_blank_rather_than_invented(self):
        row = gen._row_from_list_entry(
            entry(startedAt={"year": 2024, "month": 3, "day": None},
                  completedAt={"year": None, "month": None, "day": None}), "manga")
        self.assertEqual(row["start_date"], "")
        self.assertEqual(row["finish_date"], "")

    def test_a_reread_is_flagged_for_the_xml_builders(self):
        row = gen._row_from_list_entry(entry(status="REPEATING"), "manga")
        self.assertEqual(row["status"], "Rereading")
        self.assertTrue(row["_rereading"])

    def test_an_entry_with_no_media_is_dropped_not_half_built(self):
        self.assertIsNone(gen._row_from_list_entry({"media": {}}, "manga"))
        self.assertIsNone(gen._row_from_list_entry({}, "manga"))

    def test_an_anilist_only_entry_survives_with_no_mal_id(self):
        row = gen._row_from_list_entry(entry(media=media(idMal=None)), "manga")
        self.assertEqual(row["id"], 0)
        self.assertFalse(row["can_export_xml"])
        self.assertTrue(row["can_write_anilist"], "it is still writable back to AniList")


class CollectionReaderTests(unittest.TestCase):
    def test_a_public_list_needs_no_token(self):
        with patch.object(gen, "_request_graphql",
                          return_value=collection([group([entry()])])) as reader:
            out = gen.anilist_list_collection("manga", username="someone")
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["entries"]), 1)
        self.assertTrue(reader.called, "the unauthenticated path must be the one used")

    def test_a_custom_list_is_skipped_and_duplicates_are_dropped(self):
        pages = collection([
            group([entry()]),
            group([entry(id=2)], custom=True),      # the same media, repeated
            group([entry(id=3)]),                   # and again under another status
        ])
        with patch.object(gen, "_request_graphql", return_value=pages):
            out = gen.anilist_list_collection("manga", username="someone")
        self.assertEqual(len(out["entries"]), 1,
                         "one media id must produce exactly one row")

    def test_every_chunk_is_read(self):
        first = collection([group([entry()])], has_next=True)
        second = collection([group([entry(media=media(id=1, idMal=1))])])
        with patch.object(gen, "_request_graphql", side_effect=[first, second]):
            out = gen.anilist_list_collection("manga", username="someone")
        self.assertEqual(len(out["entries"]), 2)
        self.assertEqual(out["chunks"], 2)

    def test_a_rejected_per_chunk_halves_instead_of_giving_up(self):
        attempts = []

        def reader(query, variables, *a, **k):
            attempts.append(variables["perChunk"])
            if variables["perChunk"] > 125:
                return None
            return collection([group([entry()])])

        with patch.object(gen, "_request_graphql", side_effect=reader):
            out = gen.anilist_list_collection("manga", username="someone")
        self.assertTrue(out["ok"])
        self.assertEqual(attempts, [500, 250, 125])
        self.assertEqual(out["per_chunk"], 125)

    def test_a_name_with_no_lists_is_named_not_reported_as_empty(self):
        with patch.object(gen, "_request_graphql", return_value=collection([])):
            out = gen.anilist_list_collection("manga", username="ghost")
        self.assertFalse(out["ok"])
        self.assertIn("private", out["error"])

    def test_neither_a_name_nor_a_token_is_refused_up_front(self):
        out = gen.anilist_list_collection("manga")
        self.assertFalse(out["ok"])
        self.assertIn("username", out["error"])

    def test_a_token_reads_by_user_id_through_the_authenticated_path(self):
        with patch.object(gen, "_execute_anilist_authenticated",
                          return_value={"data": collection([group([entry()])])}) as reader:
            out = gen.anilist_list_collection("manga", token="t", user_id=7)
        self.assertTrue(out["ok"])
        self.assertEqual(reader.call_args[0][1]["userId"], 7)
        self.assertNotIn("name", reader.call_args[0][1])

    def test_the_query_asks_for_what_the_import_needs(self):
        query = gen.ANILIST_LIST_COLLECTION_QUERY
        self.assertIn("forceSingleCompletedList: true", query,
                      "split completed lists would come back as separate groups")
        self.assertIn("score (format: POINT_100)", query,
                      "a bare score arrives in the account's own display format")
        self.assertIn("isCustomList", query)
        self.assertIn("hasNextChunk", query)
        self.assertIn("idMal", query)


class ImportEndpointTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        out = Path(self._dir.name)
        env = patch.dict(os.environ, {"ANIEXPORT_CACHE": str(out / ".anilist_cache.json")})
        env.start()
        self.addCleanup(env.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), gui.MalGuiHandler)
        thread = threading.Thread(target=serve(self.server), daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = "http://127.0.0.1:%d" % self.server.server_port

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            return urllib.request.urlopen(req, timeout=20).read().decode(), 200
        except urllib.error.HTTPError as exc:
            return exc.read().decode(), exc.code

    def test_a_username_is_enough(self):
        reply = {"ok": True, "media_type": "manga",
                 "entries": [gen._row_from_list_entry(entry(), "manga")], "skipped": 0}
        with patch.object(gui, "anilist_list_collection", return_value=reply):
            body, code = self.post("/api/anilist-import",
                                   {"username": "someone", "media_type": "manga"})
        self.assertEqual(code, 200)
        payload = json.loads(body)
        # The same shape /api/parse-mal-xml returns, so the page reuses one merge.
        self.assertEqual(set(payload) >= {"ok", "media_type", "entries", "skipped"}, True)
        self.assertEqual(payload["entries"][0]["title"], "Vinland Saga")

    def test_neither_a_name_nor_a_token_is_refused(self):
        body, code = self.post("/api/anilist-import", {"media_type": "manga"})
        self.assertEqual(code, 400)

    def test_a_bad_token_is_refused_before_any_read(self):
        with patch.object(gui, "anilist_viewer",
                          return_value={"ok": False, "error": "AniList rejected the token."}), \
             patch.object(gui, "anilist_list_collection") as reader:
            body, code = self.post("/api/anilist-import", {"token": "bad", "media_type": "manga"})
        self.assertEqual(code, 401)
        reader.assert_not_called()

    def test_a_failed_read_is_surfaced_not_swallowed(self):
        with patch.object(gui, "anilist_list_collection",
                          return_value={"ok": False, "error": "nope"}):
            body, code = self.post("/api/anilist-import",
                                   {"username": "someone", "media_type": "manga"})
        self.assertEqual(code, 502)
        self.assertIn("nope", body)

    def test_anilist_only_rows_are_counted_for_the_user(self):
        rows = [gen._row_from_list_entry(entry(), "manga"),
                gen._row_from_list_entry(entry(media=media(id=9, idMal=None)), "manga")]
        with patch.object(gui, "anilist_list_collection",
                          return_value={"ok": True, "media_type": "manga",
                                        "entries": rows, "skipped": 0}):
            body, _ = self.post("/api/anilist-import",
                                {"username": "someone", "media_type": "manga"})
        self.assertEqual(json.loads(body)["anilist_only"], 1)

    def test_the_import_takes_the_run_lock(self):
        gui._run_lock.acquire()
        self.addCleanup(gui._run_lock.release)
        body, code = self.post("/api/anilist-import",
                               {"username": "someone", "media_type": "manga"})
        self.assertEqual(code, 409, "an import landing rows mid-write is a real race")

    def test_the_lock_is_released_even_when_the_read_fails(self):
        with patch.object(gui, "anilist_list_collection", side_effect=RuntimeError("boom")):
            body, code = self.post("/api/anilist-import",
                                   {"username": "someone", "media_type": "manga"})
        self.assertEqual(code, 502)
        self.assertTrue(gui._run_lock.acquire(blocking=False), "the lock was not released")
        gui._run_lock.release()


if __name__ == "__main__":
    unittest.main()
