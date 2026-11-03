import unittest

from records import dedupe_by, group_by, sort_records, total_by


class PublicTests(unittest.TestCase):
    def test_group(self):
        rows = [{"k": "b", "n": 1}, {"k": "a", "n": 2}, {"k": "b", "n": 3}]
        self.assertEqual(group_by(rows, "k"), {"b": [rows[0], rows[2]], "a": [rows[1]]})

    def test_dedupe(self):
        rows = [{"k": 1, "n": "first"}, {"k": 2}, {"k": 1, "n": "later"}]
        self.assertEqual(dedupe_by(rows, "k"), rows[:2])

    def test_sort(self):
        self.assertEqual(sort_records([{"n": 3}, {"n": 1}, {"n": 2}], "n"),
                         [{"n": 1}, {"n": 2}, {"n": 3}])

    def test_total(self):
        self.assertEqual(total_by([{"v": 1.25}, {"v": 2.5}], "v"), 3.75)
