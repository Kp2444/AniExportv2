import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import gui


class ExportStateTests(unittest.TestCase):
    """Anime and manga keep separate export files.

    Previously a single pair was shared, and switching media type posted an empty
    entry list which deleted both files. Keeping the files without separating them
    would have been worse: the download would then serve one media's bytes under the
    other's filename.
    """

    def _serve(self, directory):
        out = Path(directory)
        self.patches = [
            patch.object(gui, "EXPORT_XML_PATH", out / "a.xml"),
            patch.object(gui, "EXPORT_GZ_PATH", out / "a.xml.gz"),
            patch.object(gui, "EXPORT_MANGA_XML_PATH", out / "m.xml"),
            patch.object(gui, "EXPORT_MANGA_GZ_PATH", out / "m.xml.gz"),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        server = ThreadingHTTPServer(("127.0.0.1", 0), gui.MalGuiHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return out, f"http://127.0.0.1:{server.server_port}"

    @staticmethod
    def _post(base, path, body):
        req = urllib.request.Request(
            base + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            return urllib.request.urlopen(req, timeout=10).read().decode(), 200
        except urllib.error.HTTPError as exc:
            return exc.read().decode(), exc.code

    @staticmethod
    def _get(base, path):
        try:
            res = urllib.request.urlopen(base + path, timeout=10)
            return res.read().decode(), res.status, res.headers.get("Content-Disposition")
        except urllib.error.HTTPError as exc:
            return exc.read().decode(), exc.code, None

    def test_media_switch_keeps_files_and_serves_the_right_one(self):
        with tempfile.TemporaryDirectory() as directory:
            out, base = self._serve(directory)
            manga = [{"id": 321, "anilist_id": 654, "title": "T", "type": "Manga",
                      "chapters": 42, "volumes": 4}]
            # /api/process resolves through resolve_many_entries; patching
            # resolve_manga_entries did nothing, so this test was silently
            # making a real request to AniList on every run.
            with patch.object(gui, "resolve_many_entries", return_value={"T": manga}):
                self._post(base, "/api/process", {
                    "content": "T", "media_type": "manga", "status": "Reading", "score": 0})
            self.assertTrue((out / "m.xml").is_file())
            self.assertFalse((out / "a.xml").is_file(), "manga must not write the anime file")

            # Switching media type syncs an empty list; that must not delete anything.
            self._post(base, "/api/update-export", {
                "entries": [], "media_type": "anime", "status": "Completed", "score": 0})
            self.assertTrue((out / "m.xml").is_file(), "empty update deleted the export")

            body, status, disposition = self._get(base, "/api/download/xml?media=manga")
            self.assertEqual(status, 200)
            self.assertIn("myanimelist_manga_export.xml", disposition)
            self.assertIn("<manga>", body)

            self.assertEqual(self._get(base, "/api/download/xml?media=anime")[1], 404)
            self.assertEqual(self._get(base, "/api/download/xml?media=bogus")[1], 400)

    def test_export_state_reports_both_media(self):
        with tempfile.TemporaryDirectory() as directory:
            out, base = self._serve(directory)
            self._post(base, "/api/update-export", {
                "entries": [{"id": 5, "title": "An Anime", "type": "TV",
                             "episodes": 12, "status": "Completed"}],
                "media_type": "anime", "status": "Completed", "score": 0})
            state = json.loads(self._get(base, "/api/export-state")[0])
            self.assertTrue(state["anime"]["xml"])
            self.assertEqual(state["anime"]["count"], 1)
            self.assertEqual(state["anime"]["xml_name"], "myanimelist_export.xml")
            self.assertFalse(state["manga"]["xml"])
            self.assertEqual(state["manga"]["count"], None)

    def test_invalid_entry_is_a_400_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            _, base = self._serve(directory)
            body, status = self._post(base, "/api/update-export", {
                "entries": [{"id": 1, "title": "X", "status": "Bogus"}],
                "media_type": "manga", "status": "Reading", "score": 0})
            self.assertEqual(status, 400)
            self.assertIn("Bogus", body)

            body, status = self._post(base, "/api/update-export", {
                "entries": [{"id": 1, "title": "X", "status": "Reading",
                             "start_date": "2024-13-45"}],
                "media_type": "manga", "status": "Reading", "score": 0})
            self.assertEqual(status, 400)

    def test_mixed_statuses_export_as_written(self):
        """The reason per-row status exists: one list, several states."""
        with tempfile.TemporaryDirectory() as directory:
            out, base = self._serve(directory)
            self._post(base, "/api/update-export", {
                "entries": [
                    {"id": 1, "title": "A", "chapters": 10, "status": "Reading"},
                    {"id": 2, "title": "B", "chapters": 20, "status": "Completed"},
                    {"id": 3, "title": "C", "chapters": 30, "status": "Dropped"},
                ],
                "media_type": "manga", "status": "Reading", "score": 0})
            root = ET.parse(out / "m.xml").getroot()
            self.assertEqual(
                [m.findtext("my_status") for m in root.findall("manga")],
                ["Reading", "Completed", "Dropped"],
            )
            self.assertEqual(root.findtext("./myinfo/user_total_reading"), "1")
            self.assertEqual(root.findtext("./myinfo/user_total_completed"), "1")
            self.assertEqual(root.findtext("./myinfo/user_total_dropped"), "1")

    def test_the_page_and_the_downloads_are_never_cached(self):
        """Both change between runs: a cached page hides an update, and a cached
        download hands back the previous selection's XML."""
        with tempfile.TemporaryDirectory() as directory:
            out, base = self._serve(directory)
            page = urllib.request.urlopen(base + "/", timeout=10)
            self.assertEqual(page.headers.get("Cache-Control"), "no-store")

            self._post(base, "/api/update-export", {
                "entries": [{"id": 5, "title": "An Anime", "type": "TV",
                             "episodes": 12, "status": "Completed"}],
                "media_type": "anime", "status": "Completed", "score": 0})
            self.assertTrue((out / "a.xml").is_file())
            download = urllib.request.urlopen(base + "/api/download/xml?media=anime", timeout=10)
            self.assertEqual(download.headers.get("Cache-Control"), "no-store")


if __name__ == "__main__":
    unittest.main()
