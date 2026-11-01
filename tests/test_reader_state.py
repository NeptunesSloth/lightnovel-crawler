import unittest

from lncrawl.services.reader_state import merge


class ReaderStateMergeTests(unittest.TestCase):
    def test_positions_merge_novel_by_novel_latest_read_wins(self) -> None:
        stored = {"pos": {"a": {"id": "c1", "ts": 100}, "b": {"id": "c9", "ts": 500}}}
        incoming = {"pos": {"a": {"id": "c2", "ts": 200}, "b": {"id": "c3", "ts": 300}}}
        out = merge(stored, incoming)
        self.assertEqual(out["pos"]["a"]["id"], "c2")  # read later on this device
        self.assertEqual(out["pos"]["b"]["id"], "c9")  # read later on the other one

    def test_positions_from_both_devices_are_kept(self) -> None:
        out = merge({"pos": {"phone": {"ts": 1}}}, {"pos": {"pc": {"ts": 2}}})
        self.assertEqual(set(out["pos"]), {"phone", "pc"})

    def test_whole_sections_go_to_the_last_change(self) -> None:
        stored = {"favs": {"data": ["a", "b"], "ts": 100}, "views": {"data": {"x": {}}, "ts": 900}}
        incoming = {"favs": {"data": ["a"], "ts": 200}, "views": {"data": {}, "ts": 50}}
        out = merge(stored, incoming)
        self.assertEqual(out["favs"]["data"], ["a"])  # the removal made later wins
        self.assertEqual(out["views"]["data"], {"x": {}})  # an older copy does not

    def test_favorites_merge_item_by_item(self) -> None:
        # the phone removed "a" at 300; the PC added "b" at 200 while offline
        phone = {"favitems": {"a": {"on": False, "ts": 300}, "c": {"on": True, "ts": 50}}}
        pc = {"favitems": {"a": {"on": True, "ts": 100}, "b": {"on": True, "ts": 200}}}
        out = merge(phone, pc)["favitems"]
        self.assertFalse(out["a"]["on"])  # the later removal holds
        self.assertTrue(out["b"]["on"])  # the PC's addition is not lost
        self.assertTrue(out["c"]["on"])

    def test_stats_from_two_devices_both_kept(self) -> None:
        out = merge(
            {"stats": {"phone|2026-11-01": {"chapters": 3, "ts": 1}}},
            {"stats": {"pc|2026-11-01": {"chapters": 5, "ts": 2}}},
        )
        self.assertEqual(sorted(out["stats"]), ["pc|2026-11-01", "phone|2026-11-01"])

    def test_ignores_malformed_input(self) -> None:
        out = merge({"pos": {"a": "legacy-string"}}, {"pos": None, "favs": "nope", "favitems": []})
        self.assertEqual(out, {"pos": {}})


if __name__ == "__main__":
    unittest.main()
