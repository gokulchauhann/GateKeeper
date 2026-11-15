"""Unit tests for the statistics helpers in scripts/noisy_neighbour.py."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import noisy_neighbour as nn  # noqa: E402


class TestStatistics(unittest.TestCase):
    def test_percentile_interpolates(self):
        values = [10, 20, 30, 40, 50]
        self.assertEqual(nn.percentile(values, 0), 10)
        self.assertEqual(nn.percentile(values, 50), 30)
        self.assertEqual(nn.percentile(values, 100), 50)
        self.assertAlmostEqual(nn.percentile(values, 95), 48.0)

    def test_summary(self):
        s = nn.summary([40, 10, 30, 20])
        self.assertEqual(s["n"], 4)
        self.assertEqual(s["median_ms"], 25.0)
        self.assertEqual((s["min_ms"], s["max_ms"]), (10, 40))


if __name__ == "__main__":
    unittest.main()
