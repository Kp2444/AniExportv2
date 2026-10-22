"""Copying one AniList account onto another.

Reads every anime and every manga from a source account and writes them to the
account the destination token belongs to. The source wins where both have a
title - that is what an exact copy means - so this runs under the "overwrite"
policy rather than the never-go-backwards guard the normal write uses.
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


def row(anilist_id, media_type="manga", **over):
    base = {"id": anilist_id * 10, "anilist_id": anilist_id, "title": "T%d" % anilist_id,
            "type": "Manga" if media_type == "manga" else "TV",
            "status": "Completed", "score": 8, "comments": "note",
            "start_date": "2024-03-01", "finish_date": "2024-06-01",
            "private": False, "hidden_from_status_lists": False, "priority": 0,
            "can_export_xml": True, "can_write_anilist": True}
    if media_type == "manga":
        base.update({"chapters": 100, "volumes": 10,
                     "my_read_chapters": 100, "my_read_volumes": 10})
    else:
        base.update({"episodes": 12, "my_watched_episodes": 12})
    base.update(over)
    return base


class CopiedFieldTests(unittest.TestCase):
    """Everything that can round-trip has to actually round-trip."""

    def test_the_list_query_reads_the_anilist_only_flags(self):
        query = gen.ANILIST_LIST_COLLECTION_QUERY
        for field in ("private", "hiddenFromStatusLists", "priority"):
            self.assertIn(field, query, field)

    def test_those_flags_survive_into_a_write(self):
        source = row(1, private=True, hidden_from_status_lists=True, priority=3)
        payloads, _ = gen.iter_write_payloads([source], "manga", {}, policy="overwrite")
        sent = payloads[0]["variables"]
        self.assertIs(sent["private"], True)
        self.assertIs(sent["hiddenFromStatusLists"], True)
        self.assertEqual(sent["priority"], 3)

    def test_a_mal_xml_row_is_untouched_by_them(self):
        # A row from a MAL export has no such keys, so nothing must be sent -
        # writing private: false over a private entry would expose it.
        plain = {"anilist_id": 1, "title": "x", "status": "Completed", "chapters": 5}
        payloads, _ = gen.iter_write_payloads([plain], "manga", {})
        for field in ("private", "hiddenFromStatusLists", "priority"):
            self.assertNotIn(field, payloads[0]["variables"], field)

    def test_the_whole_field_set_a_copy_carries(self):
        source = row(1, private=True, priority=2)
        payloads, _ = gen.iter_write_payloads([source], "manga", {}, policy="overwrite")
        sent = payloads[0]["variables"]
        for field in ("mediaId", "status", "scoreRaw", "progress", "progressVolumes",
                      "notes", "startedAt", "completedAt", "private",
                      "hiddenFromStatusLists", "priority"):
            self.assertIn(field, sent, field)

    def test_a_flag_is_not_guarded_like_a_quantity(self):
        # Lowering a priority or un-privating an entry is not a regression, so
        # the never-lower rule must not reach them even under the safe policy.
        source = row(1, priority=0, private=False)
        existing = {1: {"id": 9, "mediaId": 1, "status": "COMPLETED", "progress": 100,
                        "progressVolumes": 10, "score": 80, "repeat": 0,
                        "priority": 5, "private": True}}
        payloads, _ = gen.iter_write_payloads([source], "manga", existing)
        self.assertEqual(payloads[0]["variables"]["priority"], 0)
        self.assertIs(payloads[0]["variables"]["private"], False)


class CopyStageTests(unittest.TestCase):
    def test_the_source_is_read_with_its_own_credentials(self):
        with patch.object(gui, "anilist_list_collection",
                          return_value={"ok": True, "entries": [row(1)]}) as reader, \
             patch.object(gui, "anilist_existing_entries", return_value={}):
            gui._copy_stage("manga", "someone", "", "dest-token", 7, "overwrite")
        # The destination's token says nothing about what the source published.
        self.assertEqual(reader.call_args.kwargs["username"], "someone")
        self.assertEqual(reader.call_args.kwargs["token"], "")

    def test_a_source_token_is_passed_through_for_a_private_list(self):
        with patch.object(gui, "anilist_list_collection",
                          return_value={"ok": True, "entries": []}) as reader:
            gui._copy_stage("anime", "", "src-token", "dest-token", 7, "overwrite")
        self.assertEqual(reader.call_args.kwargs["token"], "src-token")

    def test_an_unreadable_source_stops_the_stage(self):
        with patch.object(gui, "anilist_list_collection",
                          return_value={"ok": False, "error": "no such user"}):
            stage = gui._copy_stage("manga", "ghost", "", "t", 7, "overwrite")
        self.assertFalse(stage["ok"])
        self.assertIn("no such user", stage["error"])

    def test_an_unreadable_destination_stops_the_stage(self):
        with patch.object(gui, "anilist_list_collection",
                          return_value={"ok": True, "entries": [row(1)]}), \
             patch.object(gui, "anilist_existing_entries",
                          side_effect=gui.AniListReadError("HTTP 500")):
            stage = gui._copy_stage("manga", "someone", "", "t", 7, "overwrite")
        self.assertFalse(stage["ok"])
        self.assertIn("second copies", stage["error"])

    def test_an_empty_source_is_not_an_error(self):
        with patch.object(gui, "anilist_list_collection",
                          return_value={"ok": True, "entries": []}), \
             patch.object(gui, "anilist_existing_entries") as reader:
            stage = gui._copy_stage("manga", "someone", "", "t", 7, "overwrite")
        self.assertTrue(stage["ok"])
        self.assertEqual(stage["payloads"], [])
        reader.assert_not_called()

    def test_the_source_wins_under_the_copy_policy(self):
        # The destination is further along; an exact copy still takes it back.
        existing = {1: {"id": 9, "mediaId": 1, "status": "COMPLETED", "progress": 100,
                        "progressVolumes": 10, "score": 90, "repeat": 0}}
        source = row(1, status="Reading", my_read_chapters=20, my_read_volumes=2, score=0)
        with patch.object(gui, "anilist_list_collection",
                          return_value={"ok": True, "entries": [source]}), \
             patch.object(gui, "anilist_existing_entries", return_value=existing):
            stage = gui._copy_stage("manga", "someone", "", "t", 7, "overwrite")
        sent = stage["payloads"][0]["variables"]
        self.assertEqual(sent["progress"], 20, "the source wins, downgrades included")
        self.assertEqual(sent["scoreRaw"], 0)
        self.assertEqual(sent["status"], "CURRENT")


class CopyEndpointTests(unittest.TestCase):
    VIEWER = {"ok": True, "id": 7, "name": "destination-account"}

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.out = Path(self._dir.name)
        env = patch.dict(os.environ, {"ANIEXPORT_CACHE": str(self.out / ".anilist_cache.json")})
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
            return urllib.request.urlopen(req, timeout=30).read().decode(), 200
        except urllib.error.HTTPError as exc:
            return exc.read().decode(), exc.code

    def events(self, body):
        return [json.loads(c[6:]) for c in body.split(chr(10) * 2)
                if c.strip().startswith("data: ")]

    def lists(self, anime, manga):
        def reader(media_type, **kwargs):
            return {"ok": True, "entries": anime if media_type == "anime" else manga}
        return reader

    def test_both_media_types_are_copied_in_one_run(self):
        anime = [row(i, "anime") for i in (1, 2)]
        manga = [row(i, "manga") for i in (3, 4, 5)]
        seen = []

        def writer(token, batch):
            seen.extend(p["anilist_id"] for p in batch)
            return [{"ok": True, "id": 100 + p["anilist_id"]} for p in batch]

        with patch.object(gui, "anilist_viewer", return_value=self.VIEWER), \
             patch.object(gui, "anilist_list_collection", side_effect=self.lists(anime, manga)), \
             patch.object(gui, "anilist_existing_entries", return_value={}), \
             patch.object(gui, "anilist_write_batch", side_effect=writer):
            body, code = self.post("/api/anilist-copy", {
                "source_username": "someone", "token": "dest", "confirm": True})
        self.assertEqual(code, 200)
        done = self.events(body)[-1]
        self.assertEqual(done["type"], "copy_complete")
        self.assertEqual(sorted(seen), [1, 2, 3, 4, 5], "every entry of both types")
        self.assertEqual(done["stages"]["anime"]["created"], 2)
        self.assertEqual(done["stages"]["manga"]["created"], 3)
        self.assertEqual(len(done["snapshots"]), 2, "one snapshot per media type")

    def test_every_row_reports_its_media_type(self):
        with patch.object(gui, "anilist_viewer", return_value=self.VIEWER), \
             patch.object(gui, "anilist_list_collection",
                          side_effect=self.lists([row(1, "anime")], [row(2, "manga")])), \
             patch.object(gui, "anilist_existing_entries", return_value={}), \
             patch.object(gui, "anilist_write_batch",
                          side_effect=lambda t, b: [{"ok": True, "id": 1} for _ in b]):
            body, _ = self.post("/api/anilist-copy", {
                "source_username": "someone", "token": "dest", "confirm": True})
        progress = [e for e in self.events(body) if e["type"] == "write_progress"]
        self.assertEqual([e["media_type"] for e in progress], ["anime", "manga"])
        self.assertTrue(all("eta_seconds" in e for e in progress))

    def test_a_copy_needs_confirmation_and_both_identities(self):
        self.assertEqual(self.post("/api/anilist-copy", {})[1], 400)
        self.assertEqual(self.post("/api/anilist-copy", {"token": "t"})[1], 400)
        self.assertEqual(self.post("/api/anilist-copy",
                                   {"token": "t", "source_username": "s"})[1], 400)

    def test_a_bad_destination_token_is_refused_before_any_read(self):
        with patch.object(gui, "anilist_viewer",
                          return_value={"ok": False, "error": "AniList rejected the token."}), \
             patch.object(gui, "anilist_list_collection") as reader:
            body, code = self.post("/api/anilist-copy", {
                "source_username": "s", "token": "bad", "confirm": True})
        self.assertEqual(code, 401)
        reader.assert_not_called()

    def test_an_unreadable_source_stops_the_run_mid_stream(self):
        with patch.object(gui, "anilist_viewer", return_value=self.VIEWER), \
             patch.object(gui, "anilist_list_collection",
                          return_value={"ok": False, "error": "no such user"}), \
             patch.object(gui, "anilist_write_batch") as writer:
            body, _ = self.post("/api/anilist-copy", {
                "source_username": "ghost", "token": "dest", "confirm": True})
        kinds = [e["type"] for e in self.events(body)]
        self.assertIn("error", kinds)
        self.assertNotIn("copy_complete", kinds)
        writer.assert_not_called()

    def test_the_copy_takes_the_run_lock(self):
        gui._run_lock.acquire()
        self.addCleanup(gui._run_lock.release)
        body, code = self.post("/api/anilist-copy", {
            "source_username": "s", "token": "t", "confirm": True})
        self.assertEqual(code, 409)

    def test_the_preview_names_both_accounts_and_counts_both_types(self):
        anime = [row(i, "anime") for i in (1, 2)]
        manga = [row(3, "manga")]
        with patch.object(gui, "anilist_viewer", return_value=self.VIEWER), \
             patch.object(gui, "anilist_list_collection", side_effect=self.lists(anime, manga)), \
             patch.object(gui, "anilist_existing_entries", return_value={}), \
             patch.object(gui, "anilist_write_batch") as writer:
            body, code = self.post("/api/anilist-copy-preview", {
                "source_username": "someone", "token": "dest"})
        self.assertEqual(code, 200)
        plan = json.loads(body)
        self.assertEqual(plan["destination"], "destination-account")
        self.assertEqual(plan["source"], "someone")
        self.assertEqual([st["media_type"] for st in plan["stages"]], ["anime", "manga"])
        self.assertEqual([st["found"] for st in plan["stages"]], [2, 1])
        writer.assert_not_called()

    def test_the_preview_counts_what_the_destination_keeps(self):
        # A copy never deletes, so entries only the destination has survive -
        # the destination ends up a superset and the preview must say so.
        source = [row(1, "manga")]

        def reader(media_type, **kwargs):
            if media_type == "anime":
                return {"ok": True, "entries": []}
            if kwargs.get("username"):
                return {"ok": True, "entries": source}
            return {"ok": True, "entries": source + [row(99, "manga")]}

        with patch.object(gui, "anilist_viewer", return_value=self.VIEWER), \
             patch.object(gui, "anilist_list_collection", side_effect=reader), \
             patch.object(gui, "anilist_existing_entries", return_value={}):
            body, _ = self.post("/api/anilist-copy-preview", {
                "source_username": "someone", "token": "dest"})
        manga_stage = [st for st in json.loads(body)["stages"] if st["media_type"] == "manga"][0]
        self.assertEqual(manga_stage["extra_on_destination"], 1)

    def test_a_snapshot_is_written_so_a_copy_can_be_undone(self):
        with patch.object(gui, "anilist_viewer", return_value=self.VIEWER), \
             patch.object(gui, "anilist_list_collection",
                          side_effect=self.lists([], [row(1, "manga")])), \
             patch.object(gui, "anilist_existing_entries", return_value={}), \
             patch.object(gui, "anilist_write_batch",
                          side_effect=lambda t, b: [{"ok": True, "id": 555} for _ in b]):
            body, _ = self.post("/api/anilist-copy", {
                "source_username": "someone", "token": "dest", "confirm": True})
        done = self.events(body)[-1]
        self.assertEqual(len(done["snapshots"]), 1, "the empty anime stage writes none")
        snapshot = gen.load_undo_snapshot(done["snapshots"][0])
        self.assertEqual(snapshot["media_type"], "manga")
        self.assertEqual(snapshot["rows"][0]["created_entry_id"], 555,
                         "a created entry must be recorded or undo cannot remove it")

    def test_a_failed_row_is_reported_per_media_type(self):
        with patch.object(gui, "anilist_viewer", return_value=self.VIEWER), \
             patch.object(gui, "anilist_list_collection",
                          side_effect=self.lists([row(1, "anime")], [row(2, "manga")])), \
             patch.object(gui, "anilist_existing_entries", return_value={}), \
             patch.object(gui, "anilist_write_batch",
                          side_effect=lambda t, b: [{"ok": False, "error": "nope"} for _ in b]):
            body, _ = self.post("/api/anilist-copy", {
                "source_username": "someone", "token": "dest", "confirm": True})
        stages = self.events(body)[-1]["stages"]
        self.assertEqual(stages["anime"]["failed_count"], 1)
        self.assertEqual(stages["manga"]["failed_count"], 1)
        self.assertEqual(stages["manga"]["failed"][0]["title"], "T2")

    def test_the_token_never_appears_in_the_stream(self):
        secret = "super-secret-destination-token"
        with patch.object(gui, "anilist_viewer", return_value=self.VIEWER), \
             patch.object(gui, "anilist_list_collection",
                          side_effect=self.lists([], [row(1, "manga")])), \
             patch.object(gui, "anilist_existing_entries", return_value={}), \
             patch.object(gui, "anilist_write_batch",
                          side_effect=lambda t, b: [{"ok": True, "id": 1} for _ in b]):
            body, _ = self.post("/api/anilist-copy", {
                "source_username": "someone", "token": secret, "confirm": True})
        self.assertNotIn(secret, body)


if __name__ == "__main__":
    unittest.main()
