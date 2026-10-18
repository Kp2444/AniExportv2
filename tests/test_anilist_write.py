"""Direct AniList write and MAL XML import.

The write path never relies on undocumented upsert behaviour: it reads the
user's existing list entries first and supplies the entry id, so a repeated
write updates in place instead of creating a second row.
"""
import json
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


class PayloadTests(unittest.TestCase):
    def test_existing_entry_is_updated_not_duplicated(self):
        rows = [{"anilist_id": 30642, "title": "Vinland Saga", "chapters": 224,
                 "volumes": 29, "status": "Completed", "score": 9}]
        payloads, _ = iter_payloads = gen.iter_write_payloads(
            rows, "manga", {30642: {"id": 999, "mediaId": 30642}})
        self.assertEqual(len(payloads), 1)
        self.assertTrue(payloads[0]["update"])
        self.assertEqual(payloads[0]["variables"]["id"], 999,
                         "the existing entry id must be sent, or AniList may create a duplicate")

    def test_new_entry_carries_no_id(self):
        rows = [{"anilist_id": 148167, "title": "AniList only", "status": "Reading"}]
        payloads, _ = gen.iter_write_payloads(rows, "manga", {})
        self.assertNotIn("id", payloads[0]["variables"])
        self.assertFalse(payloads[0]["update"])

    def test_status_and_score_mapping(self):
        cases = {
            "Reading": "CURRENT", "Completed": "COMPLETED", "Plan to read": "PLANNING",
            "Paused": "PAUSED", "Dropped": "DROPPED", "Rereading": "REPEATING",
        }
        for status, expected in cases.items():
            with self.subTest(status=status):
                payloads, skipped = gen.iter_write_payloads(
                    [{"anilist_id": 1, "title": "x", "status": status, "score": 7}], "manga", {})
                self.assertEqual(skipped, [])
                self.assertEqual(payloads[0]["variables"]["status"], expected)
                self.assertEqual(payloads[0]["variables"]["scoreRaw"], 70)

    def test_progress_matches_the_xml_builders(self):
        row = {"anilist_id": 1, "title": "x", "status": "Completed",
               "chapters": 100, "volumes": 10}
        payloads, _ = gen.iter_write_payloads([row], "manga", {})
        self.assertEqual(payloads[0]["variables"]["progress"], 100)
        self.assertEqual(payloads[0]["variables"]["progressVolumes"], 10)

        partial = dict(row, status="Reading", my_read_chapters=42, my_read_volumes=5)
        payloads, _ = gen.iter_write_payloads([partial], "manga", {})
        self.assertEqual(payloads[0]["variables"]["progress"], 42)
        self.assertEqual(payloads[0]["variables"]["progressVolumes"], 5)

    def test_dates_become_fuzzy_dates_only_when_complete(self):
        payloads, _ = gen.iter_write_payloads(
            [{"anilist_id": 1, "title": "x", "status": "Completed",
              "start_date": "2024-03-01", "finish_date": "0000-00-00"}], "manga", {})
        self.assertEqual(payloads[0]["variables"]["startedAt"],
                         {"year": 2024, "month": 3, "day": 1})
        self.assertNotIn("completedAt", payloads[0]["variables"])

    def test_rows_without_an_anilist_id_are_skipped_with_a_reason(self):
        payloads, skipped = gen.iter_write_payloads(
            [{"anilist_id": 0, "title": "Nowhere", "status": "Reading"}], "manga", {})
        self.assertEqual(payloads, [])
        self.assertEqual(skipped[0]["title"], "Nowhere")
        self.assertIn("AniList id", skipped[0]["reason"])

    def test_anime_payloads_use_episodes_and_omit_volumes(self):
        payloads, _ = gen.iter_write_payloads(
            [{"anilist_id": 1, "title": "x", "status": "Completed", "episodes": 12}], "anime", {})
        self.assertEqual(payloads[0]["variables"]["progress"], 12)
        self.assertNotIn("progressVolumes", payloads[0]["variables"])

    def test_validation_messages_are_surfaced(self):
        errors = [{"message": "validation", "status": 400,
                   "validation": {"score": ["The score may not be greater than 100."]}}]
        self.assertEqual(gen._describe_anilist_error(errors),
                         "The score may not be greater than 100.")


class WriteEndpointTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        out = Path(self._dir.name)
        for name, value in (("EXPORT_XML_PATH", out / "a.xml"), ("EXPORT_GZ_PATH", out / "a.gz"),
                            ("EXPORT_MANGA_XML_PATH", out / "m.xml"),
                            ("EXPORT_MANGA_GZ_PATH", out / "m.gz")):
            patcher = patch.object(gui, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), gui.MalGuiHandler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            return urllib.request.urlopen(req, timeout=20).read().decode(), 200
        except urllib.error.HTTPError as exc:
            return exc.read().decode(), exc.code

    def test_token_is_required_everywhere(self):
        for path in ("/api/anilist-test", "/api/anilist-preview", "/api/anilist-write"):
            body, code = self.post(path, {"entries": []})
            self.assertEqual(code, 400, path)

    def test_write_requires_explicit_confirmation(self):
        body, code = self.post("/api/anilist-write",
                               {"token": "t", "entries": [{"anilist_id": 1}]})
        self.assertEqual(code, 400)
        self.assertIn("confirmation", body)

    def test_preview_counts_adds_and_updates(self):
        entries = [{"anilist_id": 30642, "title": "A", "status": "Reading"},
                   {"anilist_id": 148167, "title": "B", "status": "Reading"}]
        with patch.object(gui, "anilist_viewer", return_value={"ok": True, "id": 7, "name": "kevin"}), \
             patch.object(gui, "anilist_existing_entries",
                          return_value={30642: {"id": 55, "mediaId": 30642}}):
            body, code = self.post("/api/anilist-preview",
                                   {"token": "t", "media_type": "manga", "entries": entries})
        self.assertEqual(code, 200)
        plan = json.loads(body)
        self.assertEqual((plan["updates"], plan["creates"]), (1, 1))
        self.assertEqual(plan["viewer"], "kevin")

    def test_a_bad_token_is_rejected_before_anything_is_written(self):
        with patch.object(gui, "anilist_viewer",
                          return_value={"ok": False, "error": "AniList rejected the token."}):
            body, code = self.post("/api/anilist-write", {
                "token": "bad", "confirm": True, "media_type": "manga",
                "entries": [{"anilist_id": 1, "title": "x", "status": "Reading"}]})
        self.assertEqual(code, 401)

    def test_write_streams_progress_and_never_echoes_the_token(self):
        entries = [{"anilist_id": 1, "title": "A", "status": "Reading"},
                   {"anilist_id": 2, "title": "B", "status": "Reading"}]
        secret = "super-secret-token-value"
        with patch.object(gui, "anilist_viewer", return_value={"ok": True, "id": 7, "name": "kevin"}), \
             patch.object(gui, "anilist_existing_entries", return_value={1: {"id": 11, "mediaId": 1}}), \
             patch.object(gui, "anilist_write_entry", return_value={"ok": True, "id": 123}):
            body, code = self.post("/api/anilist-write", {
                "token": secret, "confirm": True, "media_type": "manga", "entries": entries})
        events = [json.loads(c[6:]) for c in body.split(chr(10) * 2)
                  if c.strip().startswith("data: ")]
        kinds = [e["type"] for e in events]
        self.assertEqual(kinds[0], "started")
        self.assertIn("plan", kinds)
        self.assertEqual(sum(1 for k in kinds if k == "write_progress"), 2)
        done = [e for e in events if e["type"] == "write_complete"][0]
        self.assertEqual((done["updated"], done["created"]), (1, 1))
        self.assertNotIn(secret, body, "the token must never appear in a response")

    def test_a_failed_entry_is_reported_not_swallowed(self):
        with patch.object(gui, "anilist_viewer", return_value={"ok": True, "id": 7, "name": "k"}), \
             patch.object(gui, "anilist_existing_entries", return_value={}), \
             patch.object(gui, "anilist_write_entry",
                          return_value={"ok": False, "error": "The score may not be greater than 100."}):
            body, _ = self.post("/api/anilist-write", {
                "token": "t", "confirm": True, "media_type": "manga",
                "entries": [{"anilist_id": 1, "title": "A", "status": "Reading"}]})
        done = [json.loads(c[6:]) for c in body.split(chr(10) * 2)
                if c.strip().startswith("data: ")][-1]
        self.assertEqual(done["type"], "write_complete")
        self.assertEqual(len(done["failed"]), 1)
        self.assertIn("score", done["failed"][0]["error"])


class ResolveOneTests(unittest.TestCase):
    """One entry point for fixing an unmatched row: title, id, or pasted link."""

    def test_links_are_parsed_exactly(self):
        cases = {
            "https://anilist.co/manga/30002/Berserk/": ("anilist", 30002),
            "anilist.co/anime/1": ("anilist", 1),
            "https://myanimelist.net/manga/642/Vinland_Saga": ("mal", 642),
        }
        for text, (kind, value) in cases.items():
            with self.subTest(text=text):
                ref = gen.parse_media_reference(text)
                self.assertEqual((ref["kind"], ref["value"]), (kind, value))

    def test_bare_number_is_reported_as_ambiguous_not_guessed(self):
        self.assertEqual(gen.parse_media_reference("642"),
                         {"kind": "digits", "value": 642})

    def test_a_link_resolves_through_the_matching_lookup(self):
        entry = {"id": 0, "anilist_id": 148167, "title": "AniList only"}
        with patch.object(gen, "lookup_by_anilist_id", return_value=entry) as by_al,              patch.object(gen, "lookup_by_mal_id") as by_mal:
            out = gen.resolve_one("https://anilist.co/manga/148167/", "manga")
        self.assertTrue(out["ok"])
        self.assertEqual(out["entries"][0]["anilist_id"], 148167)
        self.assertEqual(out["entries"][0]["match_source"], "anilist")
        by_al.assert_called_once()
        by_mal.assert_not_called()

    def test_a_bare_number_is_tried_as_both_ids(self):
        with patch.object(gen, "lookup_by_anilist_id",
                          return_value={"id": 1, "anilist_id": 642, "title": "By AniList id"}),              patch.object(gen, "lookup_by_mal_id",
                          return_value={"id": 642, "anilist_id": 30642, "title": "By MAL id"}):
            out = gen.resolve_one("642", "manga")
        self.assertEqual([e["match_source"] for e in out["entries"]], ["anilist", "mal"])

    def test_an_explicit_id_kind_narrows_the_lookup(self):
        with patch.object(gen, "lookup_by_anilist_id", return_value=None) as by_al,              patch.object(gen, "lookup_by_mal_id",
                          return_value={"id": 642, "anilist_id": 30642, "title": "x"}) as by_mal:
            out = gen.resolve_one("642", "manga", id_kind="mal")
        by_al.assert_not_called()
        by_mal.assert_called_once()
        self.assertEqual(out["entries"][0]["id"], 642)

    def test_free_text_returns_ranked_candidates(self):
        with patch.object(gen, "search_candidates",
                          return_value=[{"id": 1, "anilist_id": 2, "title": "A"},
                                        {"id": 3, "anilist_id": 4, "title": "B"}]):
            out = gen.resolve_one("something", "manga")
        self.assertEqual(len(out["entries"]), 2)
        self.assertTrue(all(e["match_source"] == "search" for e in out["entries"]))

    def test_nothing_found_is_said_plainly(self):
        with patch.object(gen, "search_candidates", return_value=[]):
            out = gen.resolve_one("Raphsody", "manga")
        self.assertFalse(out["ok"])
        self.assertIn("no manga matching", out["error"])

    def test_endpoint_rejects_an_empty_query(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            patcher = patch.object(gui, "EXPORT_MANGA_XML_PATH", out / "m.xml")
            patcher.start(); self.addCleanup(patcher.stop)
            server = ThreadingHTTPServer(("127.0.0.1", 0), gui.MalGuiHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}/api/resolve-one",
                    data=json.dumps({"query": "  ", "media_type": "manga"}).encode(),
                    headers={"Content-Type": "application/json"}, method="POST")
                try:
                    urllib.request.urlopen(req, timeout=10)
                    code = 200
                except urllib.error.HTTPError as exc:
                    code = exc.code
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)
        self.assertEqual(code, 400)


class ImportTests(unittest.TestCase):
    def _roundtrip(self, entries, media_type="manga"):
        builder = gen.build_mal_manga_xml if media_type == "manga" else gen.build_mal_xml
        return gui.parse_mal_xml(builder(entries))

    def test_manga_export_round_trips(self):
        original = [{"id": 642, "title": "Vinland Saga", "chapters": 224, "volumes": 29,
                     "status": "Reading", "score": 9, "my_read_chapters": 142,
                     "my_read_volumes": 18, "start_date": "2024-03-01",
                     "comments": "great", "tags": "seinen"}]
        parsed = self._roundtrip(original)
        self.assertEqual(parsed["media_type"], "manga")
        entry = parsed["entries"][0]
        self.assertEqual(entry["id"], 642)
        self.assertEqual(entry["title"], "Vinland Saga")
        self.assertEqual(entry["status"], "Reading")
        self.assertEqual(entry["score"], 9)
        self.assertEqual(entry["my_read_chapters"], 142)
        self.assertEqual(entry["my_read_volumes"], 18)
        self.assertEqual(entry["start_date"], "2024-03-01")
        self.assertEqual(entry["comments"], "great")
        self.assertEqual(entry["tags"], "seinen")
        self.assertTrue(entry["can_export_xml"])
        self.assertFalse(entry["can_write_anilist"], "a MAL file carries no AniList id")

    def test_anime_export_round_trips(self):
        parsed = self._roundtrip(
            [{"id": 1, "title": "Cowboy Bebop", "type": "TV", "episodes": 26,
              "status": "Completed"}], "anime")
        self.assertEqual(parsed["media_type"], "anime")
        self.assertEqual(parsed["entries"][0]["my_watched_episodes"], 26)

    def test_rubbish_input_is_rejected_clearly(self):
        for bad, needle in (("not xml at all", "valid XML"),
                            ("<other><x/></other>", "myanimelist")):
            with self.assertRaises(ValueError) as caught:
                gui.parse_mal_xml(bad)
            self.assertIn(needle, str(caught.exception))

    def test_doctype_is_refused(self):
        with self.assertRaises(ValueError):
            gui.parse_mal_xml('<!DOCTYPE foo [<!ENTITY a "b">]><myanimelist></myanimelist>')

    def test_entries_without_an_id_are_skipped(self):
        xml = ("<myanimelist><myinfo><user_export_type>2</user_export_type></myinfo>"
               "<manga><manga_mangadb_id>0</manga_mangadb_id><my_status>Reading</my_status></manga>"
               "<manga><manga_mangadb_id>5</manga_mangadb_id><my_status>Reading</my_status></manga>"
               "</myanimelist>")
        parsed = gui.parse_mal_xml(xml)
        self.assertEqual(len(parsed["entries"]), 1)
        self.assertEqual(parsed["skipped"], 1)


if __name__ == "__main__":
    unittest.main()
