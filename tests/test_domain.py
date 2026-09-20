import unittest
from pathlib import Path
from src.domain import load_domain

class DomainTest(unittest.TestCase):
    def test_fixture_is_complete(self):
        value = load_domain(Path("fixtures/domain.json"))
        self.assertEqual(value["domain"], "medicine-trace")
        self.assertGreater(len(value["entities"]), 2)
        self.assertGreater(len(value["rules"]), 2)

if __name__ == "__main__":
    unittest.main()
