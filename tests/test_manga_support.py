import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import generate_mal_xml as gen


class MangaExportTests(unittest.TestCase):
    def test_manga_xml_schema_counts_and_progress(self):
        xml = gen.build_mal_manga_xml([
            {
                "id": 12345, "anilist_id": 987, "title": "Title ]]> with XML",
                "type": "Manga", "chapters": 300, "volumes": 20,
                "status": "Completed", "score": 8,
            },
            {"id": 0, "title": "Unresolved", "type": "Unresolved",
             "chapters": 0, "volumes": 0, "status": "Completed"},
        ])
        root = ET.fromstring(xml)
        self.assertEqual(root.tag, "myanimelist")
        self.assertEqual(root.findtext("./myinfo/user_export_type"), "2")
        self.assertEqual(root.findtext("./myinfo/user_total_manga"), "1")
        self.assertEqual(len(root.findall("anime")), 0)
        mangas = root.findall("manga")
        self.assertEqual(len(mangas), 1)
        manga = mangas[0]
        self.assertEqual(manga.findtext("manga_mangadb_id"), "12345")
        self.assertEqual(manga.findtext("manga_title"), "Title ]]> with XML")
        self.assertEqual(manga.findtext("manga_chapters"), "300")
        self.assertEqual(manga.findtext("manga_volumes"), "20")
        self.assertEqual(manga.findtext("my_read_chapters"), "300")
        self.assertEqual(manga.findtext("my_read_volumes"), "20")
        self.assertEqual(manga.findtext("my_status"), "Completed")
        self.assertEqual(len(manga.findall("my_start_date")), 1)
        self.assertEqual(len(manga.findall("my_finish_date")), 1)

    def test_manga_status_aliases(self):
        root = ET.fromstring(gen.build_mal_manga_xml([
            {"id": 1, "title": "Currently reading", "status": "Watching"},
            {"id": 2, "title": "Planned", "status": "Plan to Watch"},
        ]))
        entries = root.findall("manga")
        self.assertEqual(entries[0].findtext("my_status"), "Reading")
        self.assertEqual(entries[1].findtext("my_status"), "Plan to Read")
        self.assertEqual(root.findtext("./myinfo/user_total_reading"), "1")
        self.assertEqual(root.findtext("./myinfo/user_total_plantoread"), "1")

    def test_manga_resolver_uses_manga_query_and_preserves_unknown_counts(self):
        media = {
            "id": 456, "idMal": 789, "title": {"english": "Example Manga", "romaji": "Example"},
            "format": "MANGA", "chapters": None, "volumes": None, "status": "FINISHED",
            "relations": {"edges": []},
        }
        with patch.object(gen, "_execute_anilist_request", return_value={"media": [media]}) as request:
            entries = gen.fetch_manga_info_online("Example Manga")
        self.assertEqual(len(entries), 1)
        self.assertIn("type: MANGA", request.call_args.args[0])
        self.assertEqual(entries[0]["id"], 789)
        self.assertEqual(entries[0]["anilist_id"], 456)
        self.assertEqual(entries[0]["chapters"], 0)
        self.assertEqual(entries[0]["volumes"], 0)
        self.assertEqual(entries[0]["type"], "Manga")

    def test_search_skips_candidates_without_a_mal_id(self):
        # AniList's top hit is often a dupe or spin-off with idMal: null; those
        # cannot be imported, so a lower-ranked hit with a MAL ID must win.
        page = {"media": [
            {"id": 85364, "idMal": None, "title": {"romaji": "One Punch Man"},
             "format": "MANGA", "chapters": None, "volumes": None},
            {"id": 109797, "idMal": 117, "title": {"romaji": "One Punch Man: one-shot"},
             "format": "ONE_SHOT", "chapters": 1, "volumes": 1},
            {"id": 30654, "idMal": 44347, "title": {"romaji": "One Punch-Man"},
             "format": "MANGA", "chapters": None, "volumes": None},
        ]}
        with patch.object(gen, "_execute_anilist_request", return_value=page):
            entries = gen.fetch_manga_info_online("One Punch Man")
        self.assertEqual(entries[0]["id"], 44347)
        self.assertEqual(entries[0]["anilist_id"], 30654)

    def test_search_falls_back_to_first_hit_when_none_have_mal_ids(self):
        page = {"media": [{"id": 1, "idMal": None, "title": {"romaji": "Obscure"},
                           "format": "MANGA", "chapters": None, "volumes": None}]}
        with patch.object(gen, "_execute_anilist_request", return_value=page):
            entries = gen.fetch_manga_info_online("Obscure")
        self.assertEqual(entries[0]["id"], 0)  # stays visibly unexportable

    def test_anime_xml_still_works_and_rejects_invalid_mal_id(self):
        xml = gen.build_mal_xml([
            {"id": 100, "title": "Example Anime", "type": "TV", "episodes": 12,
             "status": "Completed"},
            {"id": 0, "title": "Missing MAL ID", "type": "Unresolved", "episodes": 0,
             "status": "Completed"},
        ])
        root = ET.fromstring(xml)
        self.assertEqual(root.findtext("./myinfo/user_export_type"), "1")
        self.assertEqual(len(root.findall("anime")), 1)
        self.assertEqual(len(root.findall("manga")), 0)
        self.assertEqual(root.findtext("./anime/series_animedb_id"), "100")

    def test_status_casing_is_normalized_to_mal_canonical_values(self):
        manga = ET.fromstring(gen.build_mal_manga_xml([
            {"id": 1, "title": "Example", "status": "completed"},
            {"id": 2, "title": "Example 2", "status": "on-hold"},
        ]))
        self.assertEqual(manga.findall("manga")[0].findtext("my_status"), "Completed")
        self.assertEqual(manga.findall("manga")[1].findtext("my_status"), "On-Hold")

    def test_invalid_status_is_rejected(self):
        with self.assertRaises(ValueError):
            gen.build_mal_manga_xml([{"id": 1, "title": "Example", "status": "<bad>"}])

    def test_explicit_progress_overrides_the_derived_default(self):
        root = ET.fromstring(gen.build_mal_manga_xml([
            {"id": 1, "title": "Partly read", "chapters": 200, "volumes": 29,
             "status": "Reading", "my_read_chapters": 42, "my_read_volumes": 5},
        ]))
        manga = root.find("manga")
        self.assertEqual(manga.findtext("my_read_chapters"), "42")
        self.assertEqual(manga.findtext("my_read_volumes"), "5")

    def test_explicit_zero_progress_is_not_treated_as_missing(self):
        root = ET.fromstring(gen.build_mal_manga_xml([
            {"id": 1, "title": "Restarted", "chapters": 200, "volumes": 29,
             "status": "Completed", "my_read_chapters": 0},
        ]))
        self.assertEqual(root.find("manga").findtext("my_read_chapters"), "0")

    def test_candidates_include_anilist_only_entries(self):
        nodes = [
            {"id": 85364, "idMal": None, "title": {"romaji": "Dupe"}, "format": "MANGA"},
            {"id": 30654, "idMal": 44347, "title": {"romaji": "Real"}, "format": "MANGA",
             "chapters": 200, "volumes": 29},
            {"id": 99999, "idMal": 44347, "title": {"romaji": "Duplicate MAL id"},
             "format": "MANGA"},
        ]
        # Candidates are ranked now, so the search title decides which of two
        # nodes sharing a MAL ID survives the dedupe.
        picks = gen._candidates(nodes, "manga", "Real")
        # The MAL-linked entry still ranks first, but the AniList-only one is no
        # longer discarded: it is exportable by a direct write.
        self.assertEqual(picks[0]["id"], 44347)
        self.assertEqual(picks[0]["title"], "Real")
        self.assertTrue(picks[0]["can_export_xml"])
        anilist_only = [c for c in picks if not c["can_export_xml"]]
        self.assertEqual(len(anilist_only), 1)
        self.assertTrue(anilist_only[0]["can_write_anilist"])

    def test_entry_dates_are_emitted_and_validated(self):
        root = ET.fromstring(gen.build_mal_manga_xml([
            {"id": 1, "title": "Dated", "chapters": 10, "status": "Completed",
             "start_date": "2024-03-01", "finish_date": "2025-01-15"},
        ]))
        manga = root.find("manga")
        self.assertEqual(manga.findtext("my_start_date"), "2024-03-01")
        self.assertEqual(manga.findtext("my_finish_date"), "2025-01-15")

    def test_missing_dates_keep_the_mal_placeholder(self):
        for builder, tag in ((gen.build_mal_xml, "anime"), (gen.build_mal_manga_xml, "manga")):
            root = ET.fromstring(builder([{"id": 1, "title": "X", "status": "Completed"}]))
            node = root.find(tag)
            self.assertEqual(node.findtext("my_start_date"), "0000-00-00")
            self.assertEqual(node.findtext("my_finish_date"), "0000-00-00")

    def test_malformed_date_is_rejected(self):
        for bad in ("2024-13-45", "01/03/2024", "2024-3-1", "not a date"):
            with self.assertRaises(ValueError, msg=bad):
                gen.build_mal_manga_xml([
                    {"id": 1, "title": "X", "status": "Completed", "start_date": bad}])

    def test_deduplicate_uses_mal_id(self):
        entries = [
            {"id": 55, "title": "One"},
            {"id": 55, "title": "Different alias"},
            {"id": 0, "title": "Unknown"},
            {"id": 0, "title": "Unknown"},
        ]
        unique = gen.deduplicate_entries(entries)
        self.assertEqual(len(unique), 2)


if __name__ == "__main__":
    unittest.main()
