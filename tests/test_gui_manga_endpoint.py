import gzip
import json
import tempfile
import threading
import unittest
import urllib.request
import xml.etree.ElementTree as ET
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import gui
from tests import serve


class MangaGuiEndpointTests(unittest.TestCase):
    def test_process_endpoint_generates_manga_xml_and_gzip(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            resolved = [{
                "id": 321, "anilist_id": 654, "title": "Test Manga",
                "type": "Manga", "chapters": 42, "volumes": 4,
            }]
            # Manga writes to its own pair of files now, so redirect both pairs and
            # assert against the manga one. Redirecting the anime pair as well keeps
            # any stray write inside the temp dir instead of the repo.
            with patch.object(gui, "EXPORT_MANGA_XML_PATH", output / "export.xml"), \
                 patch.object(gui, "EXPORT_MANGA_GZ_PATH", output / "export.xml.gz"), \
                 patch.object(gui, "EXPORT_XML_PATH", output / "anime.xml"), \
                 patch.object(gui, "EXPORT_GZ_PATH", output / "anime.xml.gz"), \
                 patch.object(gui, "resolve_many_entries", return_value={"Test Manga": resolved}):
                server = ThreadingHTTPServer(("127.0.0.1", 0), gui.MalGuiHandler)
                thread = threading.Thread(target=serve(server), daemon=True)
                thread.start()
                try:
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{server.server_port}/api/process",
                        data=json.dumps({
                            "content": "Test Manga", "media_type": "manga",
                            "status": "Completed", "score": 0,
                        }).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    body = urllib.request.urlopen(request, timeout=5).read().decode("utf-8")
                    self.assertIn('"type": "complete"', body)
                    root = ET.parse(output / "export.xml").getroot()
                    self.assertEqual(len(root.findall("manga")), 1)
                    self.assertEqual(len(root.findall("anime")), 0)
                    self.assertEqual(root.findtext("./manga/manga_mangadb_id"), "321")
                    gz_root = ET.fromstring(gzip.decompress((output / "export.xml.gz").read_bytes()))
                    self.assertEqual(len(gz_root.findall("manga")), 1)
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)


    def test_progress_events_stream_resolved_rows(self):
        """Rows must reach the client as they resolve, not only at the end."""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            def fake(titles, media_type="manga", expand_sequels=False, session_cache=None,
                     use_cache=True, on_progress=None, should_cancel=None,
                       prefer_mal_id=True):
                out = {}
                for n, title in enumerate(titles, 1):
                    out[title] = [{"id": 100 + len(title), "anilist_id": 1,
                                   "title": title.upper(), "type": "Manga",
                                   "chapters": 5, "volumes": 1}]
                    if on_progress:
                        on_progress("resolve", n, len(titles),
                                    {"title": title, "entries": out[title]})
                return out
            with patch.object(gui, "EXPORT_MANGA_XML_PATH", output / "m.xml"),                  patch.object(gui, "EXPORT_MANGA_GZ_PATH", output / "m.xml.gz"),                  patch.object(gui, "resolve_many_entries", side_effect=fake):
                server = ThreadingHTTPServer(("127.0.0.1", 0), gui.MalGuiHandler)
                thread = threading.Thread(target=serve(server), daemon=True)
                thread.start()
                try:
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{server.server_port}/api/process",
                        data=json.dumps({"content": "aa" + chr(10) + "bbb",
                                         "media_type": "manga",
                                         "status": "Reading", "score": 0}).encode("utf-8"),
                        headers={"Content-Type": "application/json"}, method="POST",
                    )
                    body = urllib.request.urlopen(request, timeout=10).read().decode("utf-8")
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)
            blocks = body.split(chr(10) + chr(10))
            events = [json.loads(c[6:]) for c in blocks
                      if c.strip().startswith("data: ")]
            streamed = [e for e in events if e.get("type") == "progress" and e.get("entries")]
            self.assertEqual(len(streamed), 2, "each resolved title should stream its rows")
            self.assertEqual(streamed[0]["entries"][0]["title"], "AA")
            # streamed rows carry everything the table needs to render immediately
            for key in ("id", "anilist_id", "type", "chapters", "status", "score", "original"):
                self.assertIn(key, streamed[0]["entries"][0])
            final = [e for e in events if e.get("type") == "complete"][0]
            self.assertEqual([e["title"] for e in final["entries"]], ["AA", "BBB"])


if __name__ == "__main__":
    unittest.main()
