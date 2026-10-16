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


class MangaGuiEndpointTests(unittest.TestCase):
    def test_process_endpoint_generates_manga_xml_and_gzip(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            resolved = [{
                "id": 321, "anilist_id": 654, "title": "Test Manga",
                "type": "Manga", "chapters": 42, "volumes": 4,
            }]
            with patch.object(gui, "EXPORT_XML_PATH", output / "export.xml"), \
                 patch.object(gui, "EXPORT_GZ_PATH", output / "export.xml.gz"), \
                 patch.object(gui, "resolve_manga_entries", return_value=resolved):
                server = ThreadingHTTPServer(("127.0.0.1", 0), gui.MalGuiHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
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


if __name__ == "__main__":
    unittest.main()
