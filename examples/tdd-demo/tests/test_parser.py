import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from parser import parse_csv


class ParserTest(unittest.TestCase):
    def test_empty_field(self):
        self.assertEqual(parse_csv("a,,b"), ["a", "", "b"])


if __name__ == "__main__":
    unittest.main()
