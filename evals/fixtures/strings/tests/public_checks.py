import unittest

from strings import mask_email, parse_bool, slugify, truncate


class PublicTests(unittest.TestCase):
    def test_slug(self):
        self.assertEqual(slugify("Hello World"), "hello-world")
        self.assertEqual(slugify("A & B"), "a-b")

    def test_email(self):
        self.assertEqual(mask_email("alice@example.com"), "a***e@example.com")
        self.assertEqual(mask_email("hello@sample.org"), "h***o@sample.org")

    def test_bool(self):
        self.assertFalse(parse_bool("false"))
        self.assertTrue(parse_bool("YES"))
        self.assertTrue(parse_bool("1"))
        self.assertFalse(parse_bool("no"))

    def test_truncate(self):
        self.assertEqual(truncate("abcdefgh", 5), "ab...")
        self.assertLessEqual(len(truncate("abcdefgh", 2)), 2)
