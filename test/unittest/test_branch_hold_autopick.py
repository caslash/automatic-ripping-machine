import os
import unittest
from unittest import mock

from arm.ripper import branch_hold

BRANCHES = [{"letter": "A", "mpls": "00800.mpls"}, {"letter": "B", "mpls": "00801.mpls"},
            {"letter": "C", "mpls": "00802.mpls"}]


class AutopickTests(unittest.TestCase):
    def test_off_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ARM_BRANCH_AUTOPICK", None)
            self.assertIsNone(branch_hold.autopick(BRANCHES))

    def test_picks_the_exact_playlist(self):
        with mock.patch.dict(os.environ, {"ARM_BRANCH_AUTOPICK": "00800.mpls"}):
            self.assertEqual(branch_hold.autopick(BRANCHES)["letter"], "A")
        with mock.patch.dict(os.environ, {"ARM_BRANCH_AUTOPICK": " 00802.mpls "}):
            self.assertEqual(branch_hold.autopick(BRANCHES)["letter"], "C")

    def test_missing_or_ambiguous_falls_back_to_a_human(self):
        with mock.patch.dict(os.environ, {"ARM_BRANCH_AUTOPICK": "00900.mpls"}):
            self.assertIsNone(branch_hold.autopick(BRANCHES))
        dup = BRANCHES + [{"letter": "D", "mpls": "00800.mpls"}]
        with mock.patch.dict(os.environ, {"ARM_BRANCH_AUTOPICK": "00800.mpls"}):
            self.assertIsNone(branch_hold.autopick(dup))
        self.assertIsNone(branch_hold.autopick([{"letter": "A", "mpls": None}], "00800.mpls"))


if __name__ == "__main__":
    unittest.main()
