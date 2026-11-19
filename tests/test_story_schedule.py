import unittest

from app import framework as fw


class StorySchedule(unittest.TestCase):
    def test_default_is_monthly_and_weekly_is_a_choice(self):
        self.assertEqual(fw.story_days(None), 30)
        self.assertEqual(fw.story_days(30), 30)
        self.assertEqual(fw.story_days(7), 7)
        self.assertEqual(fw.story_days(1), 7)  # the old "always run"

    def test_never_is_skipped_and_the_rest_follow_the_scope(self):
        pos = [{"symbol": "A", "anchor_type": "position"}, {"symbol": "B", "anchor_type": "watchlist"}, {"symbol": "C", "anchor_type": "watchlist"}]
        plan = {"scope": 1, "max": 50, "over": {"A": 0, "B": 7}}
        self.assertEqual([p["symbol"] for p in fw.story_select(plan, pos)], ["B"])  # A never, B chosen explicitly, C not in scope
        plan = {"scope": 2, "max": 50, "over": {"A": 0, "C": 30}}
        self.assertEqual([p["symbol"] for p in fw.story_select(plan, pos)], ["B", "C"])

    def test_the_schedule_values_are_accepted(self):
        for v in (0, 7, 30):
            self.assertEqual(fw.validate("story_stock", v), v)
        with self.assertRaises(fw.RuleError):
            fw.validate("story_stock", 31)


if __name__ == "__main__":
    unittest.main()
