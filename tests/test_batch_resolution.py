import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import generate_mal_xml as gen


def node(mal_id, title, fmt="MANGA", popularity=100, status="FINISHED"):
    return {
        "id": 10000 + mal_id, "idMal": mal_id,
        "title": {"english": title, "romaji": title},
        "format": fmt, "chapters": 10, "volumes": 1,
        "status": status, "startDate": {"year": 2001},
        "popularity": popularity, "coverImage": {"medium": "http://x/" + title + ".jpg"},
    }


class CacheIsolatedCase(unittest.TestCase):
    """Every test gets its own cache file; none may touch the real one."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.cache_file = Path(self._dir.name) / "cache.json"
        patcher = patch.dict(os.environ, {"ANIEXPORT_CACHE": str(self.cache_file)})
        patcher.start()
        self.addCleanup(patcher.stop)


class BatchResolutionTests(CacheIsolatedCase):
    def test_one_request_per_batch_not_per_title(self):
        titles = ["t" + str(i) for i in range(17)]

        def fake_batch(query, variables, max_retries=3):
            return {f"t{i}": {"media": [node(500 + i, name)]}
                    for i, name in enumerate(variables.values())}

        with patch.object(gen, "_execute_anilist_batch", side_effect=fake_batch) as batched:
            out = gen.resolve_many_entries(titles, "manga", use_cache=False)

        # 17 titles at BATCH_SIZE 8 -> 3 documents, not 17 requests
        self.assertEqual(batched.call_count, 3)
        self.assertEqual(len(out), 17)
        self.assertEqual(list(out.keys()), titles, "order of the caller's list is preserved")
        self.assertTrue(all(v[0]["id"] > 0 for v in out.values()))

    def test_unresolved_titles_keep_the_single_resolver_shape(self):
        with patch.object(gen, "_execute_anilist_batch", return_value={}):
            out = gen.resolve_many_entries(["nothing here"], "manga", use_cache=False)
        entry = out["nothing here"][0]
        self.assertEqual(entry["id"], 0)
        self.assertEqual(entry["anilist_id"], 0)
        self.assertEqual(entry["type"], "Unresolved")
        self.assertTrue(entry["unresolved"])
        self.assertEqual(entry["chapters"], 0)
        self.assertEqual(entry["volumes"], 0)

    def test_anime_unresolved_shape_uses_episodes(self):
        with patch.object(gen, "_execute_anilist_batch", return_value={}):
            out = gen.resolve_many_entries(["nope"], "anime", use_cache=False)
        self.assertEqual(out["nope"][0]["episodes"], 0)

    def test_duplicate_titles_are_requested_once(self):
        calls = []

        def fake_batch(query, variables, max_retries=3):
            calls.append(list(variables.values()))
            return {f"t{i}": {"media": [node(1, name)]} for i, name in enumerate(variables.values())}

        with patch.object(gen, "_execute_anilist_batch", side_effect=fake_batch):
            out = gen.resolve_many_entries(["Dup", "Dup", "Dup"], "manga", use_cache=False)
        self.assertEqual(calls, [["Dup"]])
        self.assertEqual(len(out), 1)

    def test_candidates_are_attached_for_the_picker(self):
        def fake_batch(query, variables, max_retries=3):
            return {"t0": {"media": [
                {"id": 1, "idMal": None, "title": {"romaji": "Dupe"}, "format": "MANGA"},
                node(44347, "One Punch-Man"),
                node(117, "One Punch Man one-shot", fmt="ONE_SHOT"),
            ]}}

        with patch.object(gen, "_execute_anilist_batch", side_effect=fake_batch):
            out = gen.resolve_many_entries(["One Punch Man"], "manga", use_cache=False)
        entry = out["One Punch Man"][0]
        self.assertEqual(entry["id"], 44347, "the idMal-null dupe must not win")
        # AniList-only entries stay in the list now: they cannot travel in MAL
        # XML but they are writable straight to AniList.
        self.assertEqual([c["id"] for c in entry["candidates"]], [44347, 117, 0])
        anilist_only = [c for c in entry["candidates"] if not c["can_export_xml"]]
        self.assertEqual(len(anilist_only), 1)
        self.assertTrue(anilist_only[0]["can_write_anilist"])


class DestinationTests(CacheIsolatedCase):
    """AniList is the real target; MAL XML is only one of two transports."""

    @staticmethod
    def _anilist_only(mal_id=None, title="AniList Only"):
        n = node(1, title)
        n["idMal"] = mal_id
        n["id"] = 148167
        return n

    def test_an_anilist_only_title_resolves_instead_of_failing(self):
        """The batch path required a MAL id to count a title as resolved, while
        the single-title path never did. Titles that exist on AniList without a
        MAL id were reported as failures."""
        def only(query, variables, max_retries=3):
            return {"t0": {"media": [self._anilist_only()]}}

        with patch.object(gen, "_execute_anilist_batch", side_effect=only):
            out = gen.resolve_many_entries(["Gotoubun No Hanayome"], "manga", use_cache=False)
        entry = out["Gotoubun No Hanayome"][0]
        self.assertEqual(entry["id"], 0, "no MAL id")
        self.assertEqual(entry["anilist_id"], 148167)
        self.assertFalse(entry["can_export_xml"])
        self.assertTrue(entry["can_write_anilist"])
        self.assertNotIn("unresolved", entry)

    def test_a_genuine_miss_is_still_a_miss(self):
        with patch.object(gen, "_execute_anilist_batch", return_value={}):
            out = gen.resolve_many_entries(["Wisemans Grandchild"], "manga", use_cache=False)
        entry = out["Wisemans Grandchild"][0]
        self.assertFalse(entry["can_export_xml"])
        self.assertFalse(entry["can_write_anilist"])
        self.assertTrue(entry["unresolved"])

    def test_prefer_mal_id_changes_which_entry_wins(self):
        """For XML the MAL-linked entry must win; for a direct write the better
        AniList match should, since MAL ids are irrelevant to that route."""
        nodes = [
            {"id": 900, "idMal": None, "title": {"english": "Exact Title", "romaji": "Exact Title"},
             "format": "MANGA", "status": "FINISHED", "popularity": 9000},
            {"id": 901, "idMal": 55, "title": {"english": "Something Else", "romaji": "Something Else"},
             "format": "MANGA", "status": "FINISHED", "popularity": 10},
        ]
        xml_pick = gen._pick_best_media(nodes, "Exact Title", prefer_mal_id=True)
        anilist_pick = gen._pick_best_media(nodes, "Exact Title", prefer_mal_id=False)
        self.assertEqual(xml_pick["idMal"], 55, "XML needs a MAL id above all else")
        self.assertEqual(anilist_pick["id"], 900, "a direct write wants the best AniList match")

    def test_one_shot_demotion_holds_in_both_modes(self):
        boruto = [
            {"id": 1, "idMal": 5500, "title": {"romaji": "Boruto"}, "format": "ONE_SHOT",
             "status": "FINISHED", "popularity": 5000},
            {"id": 2, "idMal": 97865, "title": {"english": "Boruto: Naruto Next Generations",
                                                "romaji": "Boruto"},
             "format": "MANGA", "status": "FINISHED", "popularity": 40000},
        ]
        for prefer in (True, False):
            with self.subTest(prefer_mal_id=prefer):
                self.assertEqual(
                    gen._pick_best_media(boruto, "Boruto", prefer_mal_id=prefer)["idMal"],
                    97865, "a full series outranks a one-shot regardless of destination")

    def test_cache_is_keyed_by_destination(self):
        def one(query, variables, max_retries=3):
            return {f"t{i}": {"media": [node(321, name)]}
                    for i, name in enumerate(variables.values())}

        with patch.object(gen, "_execute_anilist_batch", side_effect=one):
            gen.resolve_many_entries(["Berserk"], "manga", prefer_mal_id=True)
        with patch.object(gen, "_execute_anilist_batch", side_effect=one) as again:
            gen.resolve_many_entries(["Berserk"], "manga", prefer_mal_id=False)
        self.assertEqual(again.call_count, 1,
                         "an XML-ranked result must not be served to an AniList run")

    def test_an_anilist_only_result_is_cached(self):
        """It used to be treated as a miss, so it was re-fetched every run."""
        def only(query, variables, max_retries=3):
            return {"t0": {"media": [self._anilist_only()]}}

        with patch.object(gen, "_execute_anilist_batch", side_effect=only):
            gen.resolve_many_entries(["Gotoubun"], "manga")
        with patch.object(gen, "_execute_anilist_batch", side_effect=only) as again:
            out = gen.resolve_many_entries(["Gotoubun"], "manga")
        self.assertEqual(again.call_count, 0)
        self.assertEqual(out["Gotoubun"][0]["anilist_id"], 148167)


class VariantRetryTests(CacheIsolatedCase):
    def test_parenthetical_title_is_recovered_on_the_second_pass(self):
        messy = "Hero?I Quit A Long Time Ago(YingXiong Zai Lin)"
        seen = []

        def fake_batch(query, variables, max_retries=3):
            asked = list(variables.values())
            seen.append(asked)
            out = {}
            for i, name in enumerate(asked):
                # only the cleaned-up form matches
                if name == "Hero?I Quit A Long Time Ago":
                    out[f"t{i}"] = {"media": [node(999, name)]}
            return out

        with patch.object(gen, "_execute_anilist_batch", side_effect=fake_batch):
            out = gen.resolve_many_entries([messy], "manga", use_cache=False)

        self.assertEqual(len(seen), 2, "one resolve pass, then one retry pass")
        self.assertEqual(out[messy][0]["id"], 999)

    def test_a_typo_is_not_recoverable(self):
        # AniList returns zero hits for misspellings; no variant rule can help.
        with patch.object(gen, "_execute_anilist_batch", return_value={}):
            out = gen.resolve_many_entries(["Raphsody"], "manga", use_cache=False)
        self.assertEqual(out["Raphsody"][0]["id"], 0)


class CacheTests(CacheIsolatedCase):
    def _resolve(self, titles, **kwargs):
        def fake_batch(query, variables, max_retries=3):
            return {f"t{i}": {"media": [node(321, name)]} for i, name in enumerate(variables.values())}
        with patch.object(gen, "_execute_anilist_batch", side_effect=fake_batch) as batched:
            out = gen.resolve_many_entries(titles, "manga", **kwargs)
        return out, batched.call_count

    def test_second_run_makes_no_network_calls(self):
        first, calls_first = self._resolve(["Berserk"])
        self.assertEqual(calls_first, 1)
        self.assertTrue(self.cache_file.is_file())

        second, calls_second = self._resolve(["Berserk"])
        self.assertEqual(calls_second, 0, "a warm cache must not hit the network")
        self.assertEqual(second["Berserk"][0]["id"], first["Berserk"][0]["id"])

    def test_no_cache_flag_bypasses_both_read_and_write(self):
        self._resolve(["Berserk"], use_cache=False)
        self.assertFalse(self.cache_file.exists())

    def test_unresolved_results_are_never_cached(self):
        with patch.object(gen, "_execute_anilist_batch", return_value={}):
            gen.resolve_many_entries(["ghost"], "manga")
        cached = gen.load_cache()
        self.assertEqual(cached, {}, "a miss must be retried on the next run")

    def test_expired_rows_are_dropped(self):
        self._resolve(["Berserk"])
        raw = json.loads(self.cache_file.read_text(encoding="utf-8"))
        for row in raw["entries"].values():
            row["ts"] -= gen.CACHE_TTL_SECONDS + 60
        self.cache_file.write_text(json.dumps(raw), encoding="utf-8")
        self.assertEqual(gen.load_cache(), {})

    def test_corrupt_cache_is_survivable(self):
        self.cache_file.write_text("{not json", encoding="utf-8")
        self.assertEqual(gen.load_cache(), {})
        out, calls = self._resolve(["Berserk"])
        self.assertEqual(calls, 1)
        self.assertEqual(out["Berserk"][0]["id"], 321)

    def test_wrong_version_is_ignored(self):
        self.cache_file.write_text(json.dumps({"version": 99, "entries": {"x": {"ts": 1, "data": []}}}),
                                   encoding="utf-8")
        self.assertEqual(gen.load_cache(), {})

    def test_saved_cache_is_valid_json_with_the_expected_shape(self):
        self._resolve(["Berserk"])
        raw = json.loads(self.cache_file.read_text(encoding="utf-8"))
        self.assertEqual(raw["version"], gen.CACHE_VERSION)
        key = next(iter(raw["entries"]))
        self.assertEqual(key, "manga:berserk:single:mal")
        self.assertIn("ts", raw["entries"][key])
        self.assertEqual(raw["entries"][key]["data"][0]["id"], 321)

    def test_cache_is_keyed_by_media_type_and_expansion(self):
        self._resolve(["Berserk"])
        _, calls = self._resolve(["Berserk"], expand_sequels=True)
        self.assertEqual(calls, 1, "a different expansion mode is a different cache key")


class ProgressTests(CacheIsolatedCase):
    def test_progress_is_reported_per_title(self):
        events = []

        def fake_batch(query, variables, max_retries=3):
            return {f"t{i}": {"media": [node(7, name)]} for i, name in enumerate(variables.values())}

        with patch.object(gen, "_execute_anilist_batch", side_effect=fake_batch):
            gen.resolve_many_entries(["a", "b", "c"], "manga", use_cache=False,
                                     on_progress=lambda stage, done, total, info: events.append((stage, done, total, info)))
        self.assertEqual([e[0] for e in events], ["resolve"] * 3)
        self.assertEqual([e[1] for e in events], [1, 2, 3])
        self.assertEqual(events[-1][2], 3)

    def test_a_broken_callback_cannot_kill_the_run(self):
        def boom(stage, done, total, info):
            raise RuntimeError("callback exploded")

        with patch.object(gen, "_execute_anilist_batch",
                          side_effect=lambda q, v, max_retries=3: {"t0": {"media": [node(7, "a")]}}):
            out = gen.resolve_many_entries(["a"], "manga", use_cache=False, on_progress=boom)
        self.assertEqual(out["a"][0]["id"], 7)


if __name__ == "__main__":
    unittest.main()
