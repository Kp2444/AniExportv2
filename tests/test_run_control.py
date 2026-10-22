import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import generate_mal_xml as gen
import gui
from tests import serve


class ServerCase(unittest.TestCase):
    def serve(self, directory):
        out = Path(directory)
        for name, value in (
            ("EXPORT_XML_PATH", out / "a.xml"), ("EXPORT_GZ_PATH", out / "a.xml.gz"),
            ("EXPORT_MANGA_XML_PATH", out / "m.xml"), ("EXPORT_MANGA_GZ_PATH", out / "m.xml.gz"),
        ):
            patcher = patch.object(gui, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        server = ThreadingHTTPServer(("127.0.0.1", 0), gui.MalGuiHandler)
        thread = threading.Thread(target=serve(server), daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return out, f"http://127.0.0.1:{server.server_port}"

    @staticmethod
    def post(base, path, body, timeout=30):
        req = urllib.request.Request(
            base + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            return urllib.request.urlopen(req, timeout=timeout).read().decode(), 200
        except urllib.error.HTTPError as exc:
            return exc.read().decode(), exc.code

    @staticmethod
    def events(body):
        return [json.loads(c[6:]) for c in body.split(chr(10) * 2)
                if c.strip().startswith("data: ")]


class RunControlTests(ServerCase):
    """A long run must be stoppable, and only one may hold the export files."""

    def _slow_resolver(self, titles, media_type="manga", expand_sequels=False,
                       session_cache=None, use_cache=True, on_progress=None,
                       should_cancel=None, prefer_mal_id=True):
        """Batched resolver that takes real time and honours cancellation."""
        out = {}
        for n, title in enumerate(titles, 1):
            if should_cancel and should_cancel():
                break
            time.sleep(0.4)
            out[title] = [{"id": 500 + len(title), "anilist_id": 1, "title": title.upper(),
                           "type": "Manga", "chapters": 3, "volumes": 1}]
            if on_progress:
                on_progress("resolve", n, len(titles), {"title": title, "entries": out[title]})
        return out

    def test_cancel_stops_the_run_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            out, base = self.serve(directory)
            titles = chr(10).join("t" + str(i) for i in range(12))
            captured = {}

            with patch.object(gui, "resolve_many_entries", side_effect=self._slow_resolver):
                def run():
                    captured["body"], captured["code"] = self.post(
                        base, "/api/process",
                        {"content": titles, "media_type": "manga",
                         "status": "Reading", "score": 0}, timeout=60)

                worker = threading.Thread(target=run)
                worker.start()
                time.sleep(1.1)
                _, code = self.post(base, "/api/run-control", {"action": "cancel"})
                self.assertEqual(code, 200)
                worker.join(20)

            kinds = [e["type"] for e in self.events(captured["body"])]
            self.assertEqual(kinds[0], "started")
            self.assertIn("cancelled", kinds)
            self.assertNotIn("complete", kinds)
            self.assertFalse((out / "m.xml").is_file(), "a cancelled run must not write files")
            self.assertFalse(gui._run_lock.locked(), "the run lock must always be released")

    def test_second_run_is_rejected_while_one_is_active(self):
        with tempfile.TemporaryDirectory() as directory:
            _, base = self.serve(directory)
            titles = chr(10).join("t" + str(i) for i in range(10))
            captured = {}

            with patch.object(gui, "resolve_many_entries", side_effect=self._slow_resolver):
                def run():
                    captured["body"], captured["code"] = self.post(
                        base, "/api/process",
                        {"content": titles, "media_type": "manga",
                         "status": "Reading", "score": 0}, timeout=60)

                worker = threading.Thread(target=run)
                worker.start()
                time.sleep(0.6)
                body, code = self.post(
                    base, "/api/process",
                    {"content": "x", "media_type": "manga", "status": "Reading", "score": 0})
                self.assertEqual(code, 409)
                self.assertIn("already in progress", body)

                self.post(base, "/api/run-control", {"action": "pause"})
                time.sleep(0.6)
                self.post(base, "/api/run-control", {"action": "resume"})
                time.sleep(0.2)
                self.post(base, "/api/run-control", {"action": "cancel"})
                worker.join(20)

            kinds = [e["type"] for e in self.events(captured["body"])]
            self.assertIn("paused", kinds)
            self.assertIn("resumed", kinds)
            self.assertFalse(gui._run_lock.locked())

    def test_control_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            _, base = self.serve(directory)
            self.assertEqual(self.post(base, "/api/run-control", {"action": "nope"})[1], 400)
            self.assertEqual(self.post(base, "/api/run-control", {"action": "cancel"})[1], 404)


class ManualCorrectionTests(ServerCase):
    def test_lookup_and_search_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            _, base = self.serve(directory)
            for bad in ("abc", "0", "-3", ""):
                self.assertEqual(
                    self.post(base, "/api/lookup", {"mal_id": bad, "media_type": "manga"})[1],
                    400, f"mal_id {bad!r} should be rejected")
            self.assertEqual(
                self.post(base, "/api/search", {"query": "   ", "media_type": "manga"})[1], 400)

            with patch.object(gui, "lookup_by_mal_id", return_value=None):
                self.assertEqual(
                    self.post(base, "/api/lookup", {"mal_id": "999999", "media_type": "manga"})[1],
                    404)

            entry = {"id": 642, "anilist_id": 30642, "title": "Vinland Saga",
                     "type": "Manga", "chapters": 224, "volumes": 29}
            with patch.object(gui, "lookup_by_mal_id", return_value=entry):
                body, code = self.post(base, "/api/lookup", {"mal_id": "642", "media_type": "manga"})
                self.assertEqual(code, 200)
                self.assertEqual(json.loads(body)["entry"]["id"], 642)

            with patch.object(gui, "search_candidates", return_value=[entry]):
                body, code = self.post(base, "/api/search", {"query": "vinland", "media_type": "manga"})
                self.assertEqual(code, 200)
                self.assertEqual(json.loads(body)["entries"][0]["title"], "Vinland Saga")


class PreviewArchiveTests(ServerCase):
    def _write(self, base, archive=False):
        return self.post(base, "/api/update-export", {
            "entries": [{"id": 1, "title": "A", "chapters": 2, "status": "Reading"}],
            "media_type": "manga", "status": "Reading", "score": 0, "archive": archive})

    @staticmethod
    def get(base, path):
        try:
            res = urllib.request.urlopen(base + path, timeout=10)
            return res.read().decode(), res.status
        except urllib.error.HTTPError as exc:
            return exc.read().decode(), exc.code

    def test_preview_returns_xml_and_404s_when_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            _, base = self.serve(directory)
            self.assertEqual(self.get(base, "/api/preview-xml?media=manga")[1], 404)
            self._write(base)
            body, code = self.get(base, "/api/preview-xml?media=manga")
            self.assertEqual(code, 200)
            self.assertIn("<manga>", json.loads(body)["xml"])
            self.assertEqual(self.get(base, "/api/preview-xml?media=bogus")[1], 400)

    def test_archive_is_written_and_only_safe_names_are_served(self):
        with tempfile.TemporaryDirectory() as directory:
            out, base = self.serve(directory)
            self._write(base, archive=True)
            archives = sorted(q.name for q in out.glob("m_*.xml"))
            self.assertEqual(len(archives), 1)

            self.assertEqual(
                self.get(base, "/api/download/xml?media=manga&archive=" + archives[0])[1], 200)
            for bad in ("../gui.py", "evil.txt", "sub/dir.xml"):
                self.assertEqual(
                    self.get(base, "/api/download/xml?media=manga&archive=" + bad)[1], 400,
                    f"{bad!r} must be rejected")
            self.assertEqual(
                self.get(base, "/api/download/xml?media=manga&archive=missing_1.xml")[1], 404)

            state = json.loads(self.get(base, "/api/export-state")[0])
            self.assertEqual(state["manga"]["archive"], archives[0])


class ClientDisconnectTests(ServerCase):
    """Closing the tab mid-resolve used to leave the run going to the end,
    holding the run lock - long enough that reopening the page and pressing
    Process was answered with "a run is already in progress".

    send_event failing is the only signal available: resolve_many_entries
    deliberately swallows whatever its progress callback raises.
    """

    BATCH_SECONDS = 0.6

    def setUp(self):
        self.batches = 0

    def _slow_batch(self, query, variables, max_retries=3):
        """A slow network, driving the real resolver.

        Mocking resolve_many_entries itself would defeat the point: the swallow
        that hides the broken pipe lives *inside* it, in the wrapper around the
        progress callback. With the real resolver in place, a raise from
        send_event goes nowhere unless send_event handles it.
        """
        self.batches += 1
        time.sleep(self.BATCH_SECONDS)
        return {"t%d" % i: {"media": [{
            "id": 900 + i, "idMal": 700 + i,
            "title": {"english": name, "romaji": name},
            "format": "MANGA", "status": "FINISHED", "chapters": 3, "volumes": 1,
            "startDate": {"year": 2001}, "popularity": 50,
            "coverImage": {"medium": "c"}}]}
            for i, name in enumerate(variables.values())}

    def _wait_for_lock(self, want, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if gui._run_lock.locked() == want:
                return True
            time.sleep(0.05)
        return gui._run_lock.locked() == want

    def test_a_vanished_client_stops_the_run(self):
        with tempfile.TemporaryDirectory() as directory:
            _, base = self.serve(directory)
            host, port = base.rsplit("/", 1)[-1].split(":")
            # Three batches of eight. Cancellation is checked between batches,
            # so noticing the disconnect during batch one saves batches two
            # and three - about three seconds of work.
            payload = json.dumps({"content": chr(10).join("t" + str(i) for i in range(24)),
                                  "media_type": "manga", "status": "Reading",
                                  "score": 0, "use_cache": False}).encode()
            request = (b"POST /api/process HTTP/1.1" + b"\r\n"
                       + b"Host: 127.0.0.1" + b"\r\n"
                       + b"Content-Type: application/json" + b"\r\n"
                       + ("Content-Length: %d" % len(payload)).encode() + b"\r\n\r\n"
                       + payload)

            with patch.object(gen, "_execute_anilist_batch", side_effect=self._slow_batch):
                sock = socket.create_connection((host, int(port)), timeout=10)
                try:
                    sock.sendall(request)
                    # Wait until the run is genuinely inside the resolver.
                    # Closing any earlier only proves that an unprotected
                    # send_event raises and takes the handler down with it -
                    # which releases the lock by crashing, and would pass
                    # whether or not the disconnect is handled at all.
                    seen = b""
                    deadline = time.monotonic() + 15
                    while b'"type": "progress"' not in seen:
                        self.assertLess(time.monotonic(), deadline,
                                        "no progress event arrived")
                        chunk = sock.recv(4096)
                        if not chunk:
                            break
                        seen += chunk
                    self.assertIn(b'"type": "progress"', seen)
                    self.assertTrue(gui._run_lock.locked())
                finally:
                    sock.close()                      # the tab goes away

                # Wait for the run to end however it ends - generously, so a
                # busy machine cannot decide the outcome. What is asserted is
                # not how fast the lock came back but whether the remaining
                # work was skipped: cancellation is checked between batches,
                # so noticing the disconnect during batch one means batches
                # two and three never start.
                self.assertTrue(self._wait_for_lock(False, 30),
                                "the run never finished at all")

            self.assertLess(
                self.batches, 3,
                "every batch still ran, so the disconnect went unnoticed - "
                "resolve_many_entries swallows what its progress callback "
                "raises, so send_event has to set the cancel flag itself")

if __name__ == "__main__":
    unittest.main()
