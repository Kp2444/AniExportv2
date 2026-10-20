"""Contract between the GUI's event stream and the resolver.

Every case here is a bug that shipped: rows that never reached the table, a
sequel silently dropped, an ETA eight times too long, and a network outage
reported to the user as "these titles do not exist".
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

import xml.etree.ElementTree as ET

import generate_mal_xml as gen
import gui


def node(mal_id, title, fmt="MANGA"):
    return {"id": 10000 + mal_id, "idMal": mal_id,
            "title": {"english": title, "romaji": title},
            "format": fmt, "chapters": 10, "volumes": 1, "status": "FINISHED",
            "startDate": {"year": 2001}, "popularity": 100,
            "coverImage": {"medium": "c.jpg"}}


class GuiContractCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        out = Path(self._dir.name)
        env = patch.dict(os.environ, {"ANIEXPORT_CACHE": str(out / "cache.json")})
        env.start()
        self.addCleanup(env.stop)
        for name, value in (("EXPORT_XML_PATH", out / "a.xml"), ("EXPORT_GZ_PATH", out / "a.gz"),
                            ("EXPORT_MANGA_XML_PATH", out / "m.xml"),
                            ("EXPORT_MANGA_GZ_PATH", out / "m.gz")):
            patcher = patch.object(gui, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.out = out
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), gui.MalGuiHandler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self._ids = {}

    def fake_batch(self, query, variables, max_retries=3):
        out = {}
        for i, name in enumerate(variables.values()):
            self._ids.setdefault(name, 500 + len(self._ids))
            out[f"t{i}"] = {"media": [node(self._ids[name], name)]}
        return out

    def process(self, content, **extra):
        body = {"content": content, "media_type": "manga", "status": "Reading", "score": 0}
        body.update(extra)
        req = urllib.request.Request(self.base + "/api/process",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        raw = urllib.request.urlopen(req, timeout=60).read().decode()
        return [json.loads(c[6:]) for c in raw.split(chr(10) * 2)
                if c.strip().startswith("data: ")]

    @staticmethod
    def complete(events):
        return [e for e in events if e["type"] == "complete"][0]

    @staticmethod
    def streamed(events):
        return [e for e in events if e.get("type") == "progress" and e.get("entries")]


class StreamingTests(GuiContractCase):
    def test_rows_stream_on_a_cold_run(self):
        with patch.object(gen, "_execute_anilist_batch", side_effect=self.fake_batch):
            events = self.process(chr(10).join(["Alpha", "Beta", "Gamma"]))
        self.assertEqual(len(self.streamed(events)), 3)
        self.assertEqual(len(self.complete(events)["entries"]), 3)

    def test_rows_still_stream_from_a_warm_cache(self):
        """A cached title used to emit a blank progress event and no row, so a
        re-run showed an empty table until the very end."""
        with patch.object(gen, "_execute_anilist_batch", side_effect=self.fake_batch):
            self.process(chr(10).join(["Alpha", "Beta"]))
        with patch.object(gen, "_execute_anilist_batch", side_effect=self.fake_batch) as batched:
            events = self.process(chr(10).join(["Alpha", "Beta"]))
        self.assertEqual(batched.call_count, 0, "warm cache must not hit the network")
        self.assertEqual(len(self.streamed(events)), 2)
        self.assertTrue(all(e.get("title") for e in events if e.get("type") == "progress"))

    def test_expanded_sequels_reach_the_client(self):
        """Sequels are appended after a title has already streamed; building the
        final list from the stream dropped them."""
        root = {"id": 10500, "idMal": 500, "title": {"romaji": "Alpha"}, "format": "MANGA",
                "chapters": 10, "volumes": 1, "status": "FINISHED",
                "relations": {"edges": [{"relationType": "SEQUEL",
                                         "node": {"id": 777, "idMal": 777,
                                                  "type": "MANGA", "status": "FINISHED"}}]}}
        sequel = {"id": 777, "idMal": 777, "title": {"romaji": "Alpha II"}, "format": "MANGA",
                  "chapters": 5, "volumes": 1, "status": "FINISHED", "relations": {"edges": []}}

        def by_id(query, variables, max_retries=3, result_key="Media"):
            return sequel if variables.get("id") == 777 else root

        with patch.object(gen, "_execute_anilist_batch", side_effect=self.fake_batch), \
             patch.object(gen, "_execute_anilist_request", side_effect=by_id):
            events = self.process("Alpha", expand_sequels=True)
        self.assertEqual([e["title"] for e in self.complete(events)["entries"]],
                         ["Alpha", "Alpha II"])


class ProgressTests(GuiContractCase):
    def test_eta_is_paced_by_batches_not_titles(self):
        with patch.object(gen, "_execute_anilist_batch", side_effect=self.fake_batch):
            events = self.process(chr(10).join("T" + str(i) for i in range(24)))
        first = next(e for e in events if e.get("type") == "progress" and "eta_seconds" in e)
        remaining = first["total"] - first["current"]
        per_title = remaining * first["interval"]
        self.assertLess(first["eta_seconds"], per_title / 2,
                        "ETA must reflect one request per batch, not per title")

    def test_progress_and_complete_agree_on_the_total(self):
        with patch.object(gen, "_execute_anilist_batch", side_effect=self.fake_batch):
            events = self.process(chr(10).join(["Dup", "Dup", "Other"]))
        progress = [e for e in events if e.get("type") == "progress"]
        self.assertEqual(progress[-1]["total"], self.complete(events)["total"])
        self.assertEqual(self.complete(events)["total"], 2, "duplicates resolve once")


class FailureReportingTests(GuiContractCase):
    def test_an_unreachable_api_is_not_reported_as_missing_titles(self):
        with patch.object(gen, "_execute_anilist_batch", return_value=None):
            events = self.process(chr(10).join(["Berserk", "Vinland Saga"]))
        self.assertTrue(any(e.get("type") == "network_error" for e in events))
        self.assertEqual(self.complete(events)["network_failures"], 2)
        self.assertTrue(all(e["id"] == 0 for e in self.complete(events)["entries"]))
        self.assertEqual(gen.load_cache(), {}, "a failed lookup must not be cached")

    def test_use_cache_false_refetches_a_wrong_match(self):
        def wrong(query, variables, max_retries=3):
            return {f"t{i}": {"media": [node(111, "WRONG")]} for i in range(len(variables))}

        def right(query, variables, max_retries=3):
            return {f"t{i}": {"media": [node(222, "RIGHT")]} for i in range(len(variables))}

        with patch.object(gen, "_execute_anilist_batch", side_effect=wrong):
            self.process("Berserk")
        with patch.object(gen, "_execute_anilist_batch", side_effect=right):
            events = self.process("Berserk", use_cache=False)
        self.assertEqual(self.complete(events)["entries"][0]["title"], "RIGHT")


class MergeAccountingTests(GuiContractCase):
    def test_a_line_absorbed_by_dedupe_is_reported(self):
        """Two pasted lines can resolve to one MAL entry. The second used to
        disappear with no explanation at all."""
        def same_entry(query, variables, max_retries=3):
            return {f"t{i}": {"media": [node(70261, "Mushoku Tensei")]}
                    for i in range(len(variables))}

        with patch.object(gen, "_execute_anilist_batch", side_effect=same_entry):
            events = self.process(chr(10).join(["Mushoku Tensei", "Mushoku Tensei LN"]))
        done = self.complete(events)
        self.assertEqual(len(done["entries"]), 1)
        self.assertEqual(len(done["merged"]), 1)
        self.assertEqual(done["merged"][0]["original"], "Mushoku Tensei LN")

    def test_nothing_is_reported_as_merged_when_nothing_merged(self):
        with patch.object(gen, "_execute_anilist_batch", side_effect=self.fake_batch):
            events = self.process(chr(10).join(["Alpha", "Beta"]))
        self.assertEqual(self.complete(events)["merged"], [])

    def test_every_pasted_line_is_accounted_for(self):
        """resolved + unresolved + merged must equal the lines pasted."""
        def mixed(query, variables, max_retries=3):
            out = {}
            for i, name in enumerate(variables.values()):
                if name == "Ghost":
                    continue                      # no match at all
                out[f"t{i}"] = {"media": [node(70261 if "Mushoku" in name else 2, name)]}
            return out

        lines = ["Mushoku Tensei", "Mushoku Tensei LN", "Berserk", "Ghost"]
        with patch.object(gen, "_execute_anilist_batch", side_effect=mixed):
            events = self.process(chr(10).join(lines))
        done = self.complete(events)
        rows = done["entries"]
        merged = len(done["merged"])
        self.assertEqual(len(rows) + merged, len(lines),
                         "rows + merged must account for every pasted line")
        self.assertEqual(sum(1 for r in rows if r["id"] <= 0), 1, "Ghost stays visible")


class ExportIntegrityTests(GuiContractCase):
    """Gaps found by probing the update-export path directly."""

    def _write(self, entries, **extra):
        body = {"entries": entries, "media_type": "manga",
                "status": "Reading", "score": 0}
        body.update(extra)
        req = urllib.request.Request(
            self.base + "/api/update-export", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        return urllib.request.urlopen(req, timeout=20).read().decode()

    def test_two_rows_on_the_same_entry_export_once(self):
        """Fixing two unmatched titles to the same series used to write the same
        MAL id twice into one file."""
        row = {"id": 642, "anilist_id": 30642, "title": "Vinland Saga",
               "chapters": 224, "volumes": 29, "status": "Reading"}
        self._write([row, dict(row, status="Completed")])
        root = ET.parse(self.out / "m.xml").getroot()
        self.assertEqual([m.findtext("manga_mangadb_id") for m in root.findall("manga")],
                         ["642"])

    def test_progress_above_the_total_is_clamped_server_side(self):
        """The browser clamps, but a hand-built payload must not get through."""
        self._write([{"id": 7, "title": "Over", "chapters": 10, "volumes": 2,
                      "status": "Reading", "my_read_chapters": 999,
                      "my_read_volumes": 99}])
        node = ET.parse(self.out / "m.xml").getroot().find("manga")
        self.assertEqual(node.findtext("my_read_chapters"), "10")
        self.assertEqual(node.findtext("my_read_volumes"), "2")

    def test_unknown_totals_do_not_clamp_progress_to_zero(self):
        self._write([{"id": 8, "title": "Ongoing", "chapters": 0, "volumes": 0,
                      "status": "Reading", "my_read_chapters": 42}])
        node = ET.parse(self.out / "m.xml").getroot().find("manga")
        self.assertEqual(node.findtext("my_read_chapters"), "42")


class BackfillTests(unittest.TestCase):
    """A list imported from MAL carries no AniList id, so without a backfill the
    direct-write route is closed to every imported row."""

    def test_mal_only_rows_gain_an_anilist_id(self):
        entries = [{"id": 642, "anilist_id": 0, "title": "Vinland Saga"},
                   {"id": 2, "anilist_id": 0, "title": "Berserk"},
                   {"id": 0, "anilist_id": 0, "title": "Nowhere"}]
        page = {"media": [
            {"id": 30642, "idMal": 642, "coverImage": {"medium": "c1"}, "startDate": {"year": 2005}},
            {"id": 30002, "idMal": 2, "coverImage": {"medium": "c2"}, "startDate": {"year": 1989}},
        ]}
        with patch.object(gen, "_execute_anilist_request", return_value=page) as req:
            filled = gen.backfill_anilist_ids(entries, "manga")
        self.assertEqual(filled, 2)
        self.assertEqual(entries[0]["anilist_id"], 30642)
        self.assertTrue(entries[0]["can_write_anilist"])
        self.assertEqual(entries[2]["anilist_id"], 0, "a row with no MAL id cannot be filled")
        self.assertEqual(req.call_count, 1, "one request per 50 rows, not one per row")

    def test_backfilled_rows_become_writable(self):
        entries = [{"id": 642, "anilist_id": 0, "title": "Vinland Saga", "status": "Reading"}]
        before, skipped_before = gen.iter_write_payloads(entries, "manga", {})
        self.assertEqual((len(before), len(skipped_before)), (0, 1))
        with patch.object(gen, "_execute_anilist_request",
                          return_value={"media": [{"id": 30642, "idMal": 642}]}):
            gen.backfill_anilist_ids(entries, "manga")
        after, skipped_after = gen.iter_write_payloads(entries, "manga", {})
        self.assertEqual((len(after), len(skipped_after)), (1, 0))
        self.assertEqual(after[0]["variables"]["mediaId"], 30642)

    def test_nothing_to_do_makes_no_request(self):
        with patch.object(gen, "_execute_anilist_request") as req:
            filled = gen.backfill_anilist_ids(
                [{"id": 1, "anilist_id": 99, "title": "already fine"}], "manga")
        self.assertEqual(filled, 0)
        req.assert_not_called()


class LinkMediaTypeTests(unittest.TestCase):
    def test_a_link_for_the_other_media_type_says_so(self):
        with patch.object(gen, "lookup_by_anilist_id", return_value=None):
            out = gen.resolve_one("https://anilist.co/anime/1", "manga")
        self.assertFalse(out["ok"])
        self.assertIn("anime", out["error"])
        self.assertIn("manga", out["error"])

    def test_a_matching_link_is_not_second_guessed(self):
        with patch.object(gen, "lookup_by_anilist_id",
                          return_value={"id": 0, "anilist_id": 5, "title": "ok"}):
            out = gen.resolve_one("https://anilist.co/manga/5", "manga")
        self.assertTrue(out["ok"])


class AnimePathTests(GuiContractCase):
    def test_anime_runs_end_to_end_without_touching_manga_files(self):
        def anime_batch(query, variables, max_retries=3):
            out = {}
            for i, name in enumerate(variables.values()):
                self._ids.setdefault(name, 700 + len(self._ids))
                out[f"t{i}"] = {"media": [{
                    "id": 9000 + self._ids[name], "idMal": self._ids[name],
                    "title": {"english": name, "romaji": name}, "format": "TV",
                    "episodes": 12, "status": "FINISHED", "startDate": {"year": 2019},
                    "popularity": 500, "coverImage": {"medium": "c.jpg"}}]}
            return out

        self.assertIn("type: ANIME", gen._search_batch_query(2, "anime"))
        self.assertIn("episodes", gen._search_batch_query(2, "anime"))
        with patch.object(gen, "_execute_anilist_batch", side_effect=anime_batch):
            events = self.process(chr(10).join(["Frieren", "Bebop"]),
                                  media_type="anime", status="Completed")
        entries = self.complete(events)["entries"]
        self.assertEqual(len(entries), 2)
        self.assertTrue(all("episodes" in e for e in entries))
        self.assertTrue(self.out.joinpath("a.xml").is_file())
        self.assertFalse(self.out.joinpath("m.xml").is_file(),
                         "an anime run must not write the manga export")


class SameOriginTests(unittest.TestCase):
    """The server is local and has no login, so any page the user has open can
    POST to it. It cannot write to AniList - the token lives in the page and is
    never stored - but it could spend the AniList rate limit through
    /api/process and overwrite the export through /api/update-export. A JSON
    body sent as text/plain is a "simple request", so CORS never preflights it
    and nothing stopped it before this guard."""

    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), gui.MalGuiHandler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_port
        self.base = "http://127.0.0.1:%d" % self.port

    def post(self, path, body, origin=None, ctype="application/json"):
        headers = {"Content-Type": ctype}
        if origin:
            headers["Origin"] = origin
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                     headers=headers, method="POST")
        try:
            return urllib.request.urlopen(req, timeout=10).getcode()
        except urllib.error.HTTPError as exc:
            return exc.code

    BODY = {"entries": [], "media_type": "anime"}

    def test_a_request_with_no_origin_is_allowed(self):
        # curl and the harness send none, and so does a non-browser client.
        self.assertEqual(self.post("/api/update-export", self.BODY), 200)

    def test_the_page_itself_is_allowed(self):
        self.assertEqual(
            self.post("/api/update-export", self.BODY,
                      origin="http://127.0.0.1:%d" % self.port), 200)

    def test_another_website_is_refused(self):
        self.assertEqual(
            self.post("/api/update-export", self.BODY, origin="https://evil.example"), 403)

    def test_the_text_plain_bypass_is_refused_too(self):
        # text/plain avoids a CORS preflight entirely, which is how a simple
        # cross-origin POST reaches a local server in the first place.
        self.assertEqual(
            self.post("/api/update-export", self.BODY,
                      origin="https://evil.example", ctype="text/plain"), 403)

    def test_every_loopback_spelling_the_page_may_use_is_allowed(self):
        # The app opens 127.0.0.1 but people type localhost, and to a browser
        # those are different origins. Blocking either breaks the page.
        for host in ("127.0.0.1", "localhost", "[::1]"):
            with self.subTest(host=host):
                self.assertEqual(
                    self.post("/api/update-export", self.BODY,
                              origin="http://%s:%d" % (host, self.port)), 200)

    def test_a_lookalike_hostname_is_not_loopback(self):
        self.assertEqual(
            self.post("/api/update-export", self.BODY,
                      origin="http://127.0.0.1.evil.example:%d" % self.port), 403)

    def test_a_non_http_origin_is_refused(self):
        self.assertEqual(
            self.post("/api/update-export", self.BODY, origin="file://"), 403)

    def test_a_different_port_on_localhost_is_still_another_origin(self):
        self.assertEqual(
            self.post("/api/update-export", self.BODY,
                      origin="http://127.0.0.1:%d" % (self.port + 1)), 403)

    def test_the_guard_covers_the_expensive_endpoints(self):
        for path in ("/api/process", "/api/run-control", "/api/anilist-write"):
            with self.subTest(path=path):
                self.assertEqual(self.post(path, {"content": "x"},
                                           origin="https://evil.example"), 403)


if __name__ == "__main__":
    unittest.main()
