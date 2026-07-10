import unittest

from src.parser import parse_csv


class ParserTest(unittest.TestCase):
    def test_regular_fields(self) -> None:
        self.assertEqual(parse_csv("a,b"), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
