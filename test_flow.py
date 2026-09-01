#!/usr/bin/env python3
"""Offline tests for kimbo's flow ordering. No network, no credentials."""

import unittest

import kimbo


class TestCamelot(unittest.TestCase):
    """Key text -> Camelot code, across the spellings real data uses."""

    def test_known_keys(self):
        for raw, want in [("Am", "8A"), ("C", "8B"), ("Ebm", "2A"),
                          ("F#", "2B"), ("Db", "3B"), ("bb", "6B"),
                          ("E minor", "9A")]:
            self.assertEqual(kimbo.to_camelot(raw), want, raw)

    def test_unicode_accidental(self):
        """Screenshot-scraped keys carry the real sharp sign, not '#'."""
        self.assertEqual(kimbo.to_camelot("A\u266fm"), "3A")

    def test_unparseable_keys(self):
        for raw in ("", "?", None, "H"):
            self.assertIsNone(kimbo.to_camelot(raw), repr(raw))


class TestKeyPenalty(unittest.TestCase):
    """Distance around the Camelot wheel."""

    def test_exact_and_relative(self):
        self.assertEqual(kimbo.key_penalty("8A", "8A"), 0.0)
        self.assertEqual(kimbo.key_penalty("8A", "8B"), 0.25)

    def test_neighbours_and_far_side(self):
        self.assertAlmostEqual(kimbo.key_penalty("8A", "9A"), 0.15, places=6)
        self.assertAlmostEqual(kimbo.key_penalty("8A", "9B"), 0.4, places=6)
        self.assertAlmostEqual(kimbo.key_penalty("8A", "2A"), 0.9, places=6)

    def test_wheel_wraps(self):
        self.assertAlmostEqual(kimbo.key_penalty("1A", "12A"), 0.15, places=6)

    def test_unknown_key(self):
        self.assertEqual(kimbo.key_penalty(None, "8A"), 0.3)


class TestBpm(unittest.TestCase):
    """Tempo distance, half/double-time aware."""

    def test_exact_match(self):
        self.assertEqual(kimbo.bpm_penalty(120, 120), 0.0)

    def test_half_and_double_time_are_free(self):
        self.assertEqual(kimbo.bpm_penalty(174, 87), 0.0)
        self.assertEqual(kimbo.bpm_penalty(87, 174), 0.0)

    def test_small_gap(self):
        self.assertEqual(round(kimbo.bpm_penalty(100, 106), 3), 0.168)

    def test_unknown_tempo(self):
        self.assertEqual(kimbo.bpm_penalty(None, 120), 0.3)

    def test_ratio_direction(self):
        # The ratio scales the SECOND argument: 87 * 2.0 / 174 == 1.
        self.assertEqual(kimbo.bpm_gap(174, 87)[1], 2.0)
        self.assertEqual(kimbo.bpm_gap(87, 174)[1], 0.5)


if __name__ == "__main__":
    unittest.main()
