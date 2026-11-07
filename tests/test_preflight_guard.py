"""
Unit and regression tests for Pre-flight Guard and Schema Auditor
"""

import os
import sys
import unittest

# Ensure project root is in sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from preflight_guard import load_ground_truth, screen_vacancy_notice

class TestPreflightGuard(unittest.TestCase):

    def setUp(self):
        self.profile = load_ground_truth()

    def test_profile_schema_keys(self):
        """Verify that profile configuration loads properly with required keys."""
        self.assertIn("candidate", self.profile)
        self.assertIn("languages", self.profile)
        self.assertIn("experience_breakdown", self.profile)

    def test_screen_vacancy_mandatory_psc_blocker(self):
        """Ensure vacancies requiring existing SECRET UE clearance trigger a hard blocker."""
        mock_notice = "Candidates must hold a valid Personnel Security Clearance (SECRET UE) at application time."
        result = screen_vacancy_notice(mock_notice)
        self.assertFalse(result["eligible"])
        self.assertTrue(any("Personnel Security Clearance" in b for b in result["blockers"]))

    def test_screen_vacancy_eligible_clean(self):
        """Ensure standard ICT positions without disqualifying clauses pass screening."""
        mock_notice = (
            "The European Commission is seeking an IT Systems Engineer (FG IV). "
            "Profile requires at least 3 years in IT operations and working knowledge of English."
        )
        result = screen_vacancy_notice(mock_notice)
        self.assertTrue(result["eligible"])
        self.assertEqual(len(result["blockers"]), 0)

if __name__ == "__main__":
    unittest.main()
