import unittest

from number_ops import clamp, mean, paginate, safe_divide


class PublicTests(unittest.TestCase):
    def test_clamp(self):
        self.assertEqual(clamp(-3, 0, 10), 0)
        self.assertEqual(clamp(5, 0, 10), 5)
        self.assertEqual(clamp(13, 0, 10), 10)

    def test_divide(self):
        self.assertEqual(safe_divide(8, 0, 99), 99)
        self.assertEqual(safe_divide(7, 2), 3.5)

    def test_mean(self):
        self.assertEqual(mean([2, 4, 6]), 4)
        self.assertEqual(mean([5]), 5)

    def test_page(self):
        self.assertEqual(paginate([1, 2, 3, 4], 1, 2), [1, 2])
        self.assertEqual(paginate([1, 2, 3, 4], 2, 2), [3, 4])
