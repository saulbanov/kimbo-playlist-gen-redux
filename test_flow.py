#!/usr/bin/env python3
"""Offline tests for kimbo's flow ordering. No network, no credentials."""

import unittest

import kimbo


def track(key, tempo, energy=None, title="Song", artist="Artist"):
    """A track dict shaped the way the flow engine expects one."""
    return {"title": title, "artist": artist, "album": "", "tempo": tempo,
            "key": key or "", "camelot": kimbo.to_camelot(key),
            "energy": energy, "vibe": "", "source": ""}


def mean_cost(tracks, order):
    """Average key+tempo cost across consecutive pairs, arc ignored."""
    costs = [kimbo.transition_cost(tracks[a], tracks[b], None, None)
             for a, b in zip(order, order[1:])]
    return sum(costs) / len(costs)


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


class TestArcTargets(unittest.TestCase):
    """The energy curve each arc asks the ordering to follow."""

    def test_party_interpolates(self):
        targets = kimbo.arc_targets("party", 3)
        self.assertAlmostEqual(targets[0], 2.0, places=6)
        self.assertAlmostEqual(targets[1], 3 + 2 * (0.5 - 0.15) / 0.55, places=6)
        self.assertAlmostEqual(targets[2], 2.5, places=6)

    def test_party_hits_the_peak_breakpoint(self):
        self.assertAlmostEqual(kimbo.arc_targets("party", 41)[28], 5.0, places=6)

    def test_flat_has_no_arc(self):
        self.assertEqual(kimbo.arc_targets("flat", 4), [None] * 4)


class TestEffectiveEnergies(unittest.TestCase):
    """Tagged energy beats the tempo proxy; the proxy beats nothing."""

    def test_tagged_energy_wins(self):
        tracks = [track("C", 100), track("D", 120, 4), track("E", 140)]
        self.assertEqual(kimbo.effective_energies(tracks), [3.0, 4.0, 3.0])

    def test_tempo_proxy_when_untagged(self):
        tracks = [track("C", 100), track("D", 150), track("E", 125)]
        self.assertEqual(kimbo.effective_energies(tracks), [1.0, 5.0, 3.0])

    def test_no_data_is_neutral(self):
        tracks = [track("", None), track("", None)]
        self.assertEqual(kimbo.effective_energies(tracks), [3.0, 3.0])


class TestOrdering(unittest.TestCase):
    """The greedy engine, on cases whose answer is checkable by hand."""

    def test_three_tracks_take_the_cheap_join(self):
        # From C/120: G/122 costs ~0.198, Em/176 ~1.295. G goes second.
        tracks = [track("C", 120), track("Em", 176), track("G", 122)]
        self.assertEqual(kimbo.order_tracks(tracks, "flat"), [0, 2, 1])

    def test_empty_and_single(self):
        self.assertEqual(kimbo.order_tracks([], "party"), [])
        self.assertEqual(kimbo.order_tracks([track("C", 120)], "party"), [0])

    def test_scrambled_eight(self):
        tracks = [track(k, t, e) for k, t, e in
                  [("Am", 128, 4), ("F", 92, 2), ("Em", 174, 5), ("C", 100, 2),
                   ("G", 87, 1), ("Bm", 140, 4), ("Eb", 118, 3), ("Dm", 96, 2)]]
        order = kimbo.order_tracks(tracks, "flat")
        self.assertEqual(sorted(order), list(range(8)))
        self.assertEqual(order, kimbo.order_tracks(tracks, "flat"))
        self.assertLess(mean_cost(tracks, order),
                        mean_cost(tracks, list(range(8))))


if __name__ == "__main__":
    unittest.main()
