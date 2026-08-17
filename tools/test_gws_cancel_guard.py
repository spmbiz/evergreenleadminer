#!/usr/bin/env python3
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import gws_fleet_preaggregate_ownership_guard as guard


class ExplicitCancelGuardRegression(unittest.TestCase):
    def test_matching_run_id_is_blocked_before_aggregate(self):
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "123456"}, clear=False), patch.object(guard, "_explicit_cancel_target", return_value="123456"):
            with self.assertRaises(SystemExit):
                guard.assert_not_explicitly_cancelled()

    def test_different_run_id_is_not_blocked(self):
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "123456"}, clear=False), patch.object(guard, "_explicit_cancel_target", return_value="999999"):
            guard.assert_not_explicitly_cancelled()


if __name__ == "__main__":
    unittest.main(verbosity=2)
