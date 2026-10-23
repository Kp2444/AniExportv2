"""The command line path.

It was rewritten onto the batched resolver and barely exercised since; these
cover argument handling, both media types, input discovery and the error paths.
"""
import io
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import generate_mal_xml as gen


def node(mal_id, title, media_type="manga"):
    base = {"id": 9000 + mal_id, "idMal": mal_id,
            "title": {"english": title, "romaji": title},
            "format": "MANGA" if media_type == "manga" else "TV",
            "status": "FINISHED", "startDate": {"year": 2001},
            "popularity": 100, "coverImage": {"medium": "c.jpg"}}
    if media_type == "manga":
        base.update({"chapters": 10, "volumes": 2})
    else:
        base.update({"episodes": 12})
    return base


class CliCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.dir = Path(self._dir.name)
        env = patch.dict(os.environ, {"ANIEXPORT_CACHE": str(self.dir / "cache.json")})
        env.start()
        self.addCleanup(env.stop)
        self._cwd = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, self._cwd)
        self.media = "manga"

    def batch(self, query, variables, max_retries=3):
        return {f"t{i}": {"media": [node(500 + i, name, self.media)]}
                for i, name in enumerate(variables.values())}

    def run_cli(self, argv, titles=None, batch=None):
        if titles is not None:
            (self.dir / "list.txt").write_text(chr(10).join(titles), encoding="utf-8")
        buffer = io.StringIO()
        with patch.object(sys, "argv", ["generate_mal_xml.py"] + argv), \
             patch.object(gen, "_execute_anilist_batch", side_effect=batch or self.batch), \
             redirect_stdout(buffer):
            gen.main()
        return buffer.getvalue()


class MangaCliTests(CliCase):
    def test_manga_export_end_to_end(self):
        out = self.run_cli(["list.txt", "--media-type", "manga", "--status", "Reading",
                            "--score", "7", "-o", "out.xml", "-g", "out.xml.gz"],
                           titles=["Berserk", "Vagabond"])
        self.assertTrue((self.dir / "out.xml").is_file())
        self.assertTrue((self.dir / "out.xml.gz").is_file())
        root = ET.parse(self.dir / "out.xml").getroot()
        entries = root.findall("manga")
        self.assertEqual(len(entries), 2)
        self.assertEqual(root.findtext("./myinfo/user_export_type"), "2")
        self.assertEqual(entries[0].findtext("my_status"), "Reading")
        self.assertEqual(entries[0].findtext("my_score"), "7")
        self.assertIn("Exportable manga entries: 2", out)

    def test_status_vocabulary_is_translated(self):
        self.run_cli(["list.txt", "--media-type", "manga", "--status", "Plan to watch",
                      "-o", "out.xml", "-g", "out.gz"], titles=["Berserk"])
        node_ = ET.parse(self.dir / "out.xml").getroot().find("manga")
        self.assertEqual(node_.findtext("my_status"), "Plan to Read")

    def test_an_unsupported_status_fails_loudly(self):
        with self.assertRaises(ValueError):
            self.run_cli(["list.txt", "--media-type", "manga", "--status", "Nonsense",
                          "-o", "out.xml", "-g", "out.gz"], titles=["Berserk"])

    def test_unresolved_titles_are_reported_and_omitted(self):
        def nothing(query, variables, max_retries=3):
            return {}
        out = self.run_cli(["list.txt", "--media-type", "manga", "-o", "out.xml", "-g", "out.gz"],
                           titles=["Ghost"], batch=nothing)
        root = ET.parse(self.dir / "out.xml").getroot()
        self.assertEqual(len(root.findall("manga")), 0)
        self.assertIn("not found: 1", out)
        self.assertIn("Ghost", out, "the CLI should name what it could not find")

    def test_anilist_only_titles_are_not_called_missing(self):
        """A title that exists on AniList without a MAL id cannot go in the XML,
        but calling it 'missing' blames the list for a format limitation."""
        def anilist_only(query, variables, max_retries=3):
            return {"t0": {"media": [{
                "id": 148167, "idMal": None, "title": {"romaji": "AniList only"},
                "format": "MANGA", "chapters": 1, "volumes": 1, "status": "FINISHED",
                "startDate": {"year": 2021}, "popularity": 10,
                "coverImage": {"medium": "c"}}]}}

        out = self.run_cli(["list.txt", "--media-type", "manga", "-o", "out.xml", "-g", "out.gz"],
                           titles=["Alpha"], batch=anilist_only)
        self.assertIn("on AniList without a MAL ID: 1", out)
        self.assertIn("not found: 0", out)
        self.assertIn("AniList 148167", out, "the id the user needs should be printed")
        self.assertEqual(len(ET.parse(self.dir / "out.xml").getroot().findall("manga")), 0)


class AnimeCliTests(CliCase):
    def setUp(self):
        super().setUp()
        self.media = "anime"

    def test_anime_export_end_to_end(self):
        out = self.run_cli(["list.txt", "--status", "Completed", "-o", "out.xml", "-g", "out.gz"],
                           titles=["Cowboy Bebop", "Frieren"])
        root = ET.parse(self.dir / "out.xml").getroot()
        self.assertEqual(root.findtext("./myinfo/user_export_type"), "1")
        self.assertEqual(len(root.findall("anime")), 2)
        self.assertEqual(root.find("anime").findtext("series_episodes"), "12")
        self.assertIn("XML validation passed", out)

    def test_anime_is_the_default_media_type(self):
        self.run_cli(["list.txt", "-o", "out.xml", "-g", "out.gz"], titles=["Cowboy Bebop"])
        self.assertIsNotNone(ET.parse(self.dir / "out.xml").getroot().find("anime"))


class InputHandlingTests(CliCase):
    def test_input_flag_is_equivalent_to_the_positional(self):
        self.run_cli(["-i", "list.txt", "--media-type", "manga", "-o", "out.xml", "-g", "out.gz"],
                     titles=["Berserk"])
        self.assertEqual(len(ET.parse(self.dir / "out.xml").getroot().findall("manga")), 1)

    def test_notes_txt_is_found_without_an_argument(self):
        (self.dir / "notes.txt").write_text("Berserk", encoding="utf-8")
        self.run_cli(["--media-type", "manga", "-o", "out.xml", "-g", "out.gz"])
        self.assertEqual(len(ET.parse(self.dir / "out.xml").getroot().findall("manga")), 1)

    def test_numbering_and_comments_are_stripped(self):
        seen = {}

        def capture(query, variables, max_retries=3):
            seen["titles"] = list(variables.values())
            return self.batch(query, variables, max_retries)

        self.run_cli(["list.txt", "--media-type", "manga", "-o", "out.xml", "-g", "out.gz"],
                     titles=["# a comment", "1. Berserk", "- Vagabond", '"Monster"', "   "],
                     batch=capture)
        self.assertEqual(seen["titles"], ["Berserk", "Vagabond", "Monster"])

    def test_a_missing_file_is_a_clean_error(self):
        with self.assertRaises(FileNotFoundError):
            self.run_cli(["nope.txt", "--media-type", "manga"])

    def test_an_empty_file_exits_with_a_message(self):
        (self.dir / "empty.txt").write_text("   ", encoding="utf-8")
        buffer = io.StringIO()
        with patch.object(sys, "argv", ["generate_mal_xml.py", "empty.txt", "--media-type", "manga"]), \
             redirect_stdout(buffer), self.assertRaises(SystemExit) as exit_info:
            gen.main()
        self.assertEqual(exit_info.exception.code, 1)
        self.assertIn("No manga titles found", buffer.getvalue())

    def test_output_directory_is_used_when_present(self):
        (self.dir / "output").mkdir()
        self.run_cli(["list.txt", "--media-type", "manga"], titles=["Berserk"])
        self.assertTrue((self.dir / "output" / "myanimelist_manga_export.xml").is_file())

    def test_default_filenames_differ_per_media_type(self):
        self.run_cli(["list.txt", "--media-type", "manga"], titles=["Berserk"])
        self.assertTrue((self.dir / "myanimelist_manga_export.xml").is_file())
        self.assertFalse((self.dir / "myanimelist_export.xml").is_file())


class CacheFlagTests(CliCase):
    def test_a_second_run_uses_the_cache(self):
        calls = {"n": 0}

        def counting(query, variables, max_retries=3):
            calls["n"] += 1
            return self.batch(query, variables, max_retries)

        self.run_cli(["list.txt", "--media-type", "manga", "-o", "a.xml", "-g", "a.gz"],
                     titles=["Berserk"], batch=counting)
        first = calls["n"]
        self.run_cli(["list.txt", "--media-type", "manga", "-o", "b.xml", "-g", "b.gz"],
                     titles=["Berserk"], batch=counting)
        self.assertEqual(first, 1)
        self.assertEqual(calls["n"], 1, "the warm cache must not hit the network again")

    def test_no_cache_disables_both_read_and_write(self):
        calls = {"n": 0}

        def counting(query, variables, max_retries=3):
            calls["n"] += 1
            return self.batch(query, variables, max_retries)

        for name in ("a", "b"):
            self.run_cli(["list.txt", "--media-type", "manga", "--no-cache",
                          "-o", f"{name}.xml", "-g", f"{name}.gz"],
                         titles=["Berserk"], batch=counting)
        self.assertEqual(calls["n"], 2, "--no-cache must refetch every run")
        self.assertFalse((self.dir / "cache.json").exists())


class VersionTests(unittest.TestCase):
    """The User-Agent goes out on every AniList request. It used to name a
    different product ("MALAnimeExporter/2.0") and carry a version number
    nothing else in the project knew about."""

    def test_one_version_feeds_the_user_agent_and_both_entry_points(self):
        import gui
        self.assertEqual(gui.__version__, gen.__version__,
                         "the GUI must report the same version as the module it runs on")
        self.assertIn(gen.__version__, gen.USER_AGENT)
        self.assertTrue(gen.USER_AGENT.startswith("AniExport/"),
                        "the agent names this product: " + gen.USER_AGENT)

    def test_no_request_still_sends_the_old_product_name(self):
        for path in ("generate_mal_xml.py", "gui.py"):
            body = io.open(path, encoding="utf-8").read()
            self.assertNotIn("MALAnimeExporter", body, path)


if __name__ == "__main__":
    unittest.main()
