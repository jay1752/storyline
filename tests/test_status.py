"""Status against the user's own rules, decided in code (app/ai/status.py). Pure-function tests: no database, no AI.
Run: python -m unittest tests.test_status -v   (from the repo root)"""

import unittest
from datetime import date

from app.ai.status import compute_status, speed_one, speed_two, NO_DATA_LABEL


def F(key, status, raw=None, value="x", threshold="> 1", label=None, note=None, limit=10.0):
    return {"key": key, "label": label or key, "value": value, "threshold": threshold, "status": status, "raw": raw, "note": note, "limit": limit}


KEYS = ["roce", "de", "pledge", "sales_growth", "opm", "promoter", "interest_cover", "current_ratio", "quick_ratio", "cfo_pat"]


def all_pass(**override):
    """Ten rules, all passing, each with its own raw figure; override {key: filter} to change one."""
    fl = {k: F(k, "pass", raw=float(i + 1), value=str(i + 1)) for i, k in enumerate(KEYS)}
    fl.update(override)
    return list(fl.values())


FACTS = {"growth_last_4q": [35.0, 40.0, 38.0, 36.0], "consecutive_quarters_negative_sales_growth": 0, "opm_drop_from_peak_bps": 100}
TODAY = date(2026, 11, 17)


def status(filters, prev=None, facts=FACTS, prev_facts=FACTS, **kw):
    kw.setdefault("owned", True)
    kw.setdefault("today", TODAY)
    return compute_status(filters, prev if prev is not None else all_pass(), facts, prev_facts, **kw)


class Mapping(unittest.TestCase):
    def test_owned_all_pass_is_add_rules_met(self):
        r = status(all_pass(), weight_pct=3.0, sector_pct=10.0)
        self.assertEqual((r["code"], r["label"]), ("BUY", "Add rules met"))

    def test_owned_entry_rule_failing_without_exit_rule_is_within_your_rules(self):
        r = status(all_pass(sales_growth=F("sales_growth", "fail", raw=12.0, value="12%", threshold="> 30%", label="Sales growth")))
        self.assertEqual(r["code"], "HOLD")
        self.assertIn("No exit rule is triggered", r["reasons"][0]["text"])

    def test_watchlist_all_pass_is_add_rules_met_and_failing_is_not_yet(self):
        self.assertEqual(status(all_pass(), owned=False)["code"], "BUY")
        r = status(all_pass(opm=F("opm", "fail", raw=5.0, value="5%", threshold="> 30%", label="Operating margin")), owned=False)
        self.assertEqual((r["code"], r["label"]), ("WATCH", "Not yet meeting your rules"))

    def test_watchlist_never_gets_exit_or_trim(self):
        bad = all_pass(roce=F("roce", "fail", raw=5.0, value="5%", threshold="> 25%"), pledge=F("pledge", "fail", raw=40.0, value="40%"))
        r = status(bad, prev=bad, owned=False, weight_pct=50.0)
        self.assertEqual(r["code"], "WATCH")

    def test_position_over_largest_size_is_trim(self):
        r = status(all_pass(), weight_pct=9.7, sz={"size_max": 8.0, "size_target": 5.0})
        self.assertEqual(r["code"], "TRIM")
        self.assertIn("9.7%", r["reasons"][0]["text"])

    def test_exactly_at_largest_size_is_not_trim(self):
        self.assertEqual(status(all_pass(), weight_pct=8.0, sz={"size_max": 8.0})["code"], "BUY")

    def test_sector_over_limit_is_trim_for_a_position_at_least_trim_size(self):
        r = status(all_pass(), weight_pct=6.0, sector_pct=41.0, sector="Financial Services", sz={"sector_max": 30.0, "size_target": 5.0})
        self.assertEqual(r["code"], "TRIM")
        self.assertIn("Financial Services", r["reasons"][0]["text"])

    def test_small_position_in_an_overweight_sector_gets_a_note_only(self):
        r = status(all_pass(), weight_pct=2.0, sector_pct=41.0, sector="Financial Services", sz={"sector_max": 30.0, "size_target": 5.0})
        self.assertEqual(r["code"], "BUY")
        self.assertTrue(any(x["kind"] == "note" and "Financial Services" in x["text"] for x in r["reasons"]))

    def test_user_limits_are_used_not_defaults(self):
        self.assertEqual(status(all_pass(), weight_pct=9.7, sz={"size_max": 12.0})["code"], "BUY")
        self.assertEqual(status(all_pass(), weight_pct=9.7, sz={"size_max": 9.0})["code"], "TRIM")

    def test_lot_bought_this_quarter_blocks_add_rules_met(self):
        r = status(all_pass(), last_lot_date="2026-10-05", weight_pct=3.0)
        self.assertEqual(r["code"], "HOLD")
        self.assertIn("one lot per quarter", r["reasons"][0]["text"])
        self.assertEqual(status(all_pass(), last_lot_date="2026-08-05", weight_pct=3.0)["code"], "BUY")
        self.assertEqual(status(all_pass(), last_lot_date="2026-10-05", weight_pct=3.0, sz={"lot_gap": 0})["code"], "BUY")

    def test_evidence_quarters_the_user_requires_must_be_held(self):
        thin = {**FACTS, "growth_last_4q": [35.0, None, None, 36.0]}
        self.assertEqual(status(all_pass(), facts=thin, sz={"evidence_quarters": 3})["code"], "HOLD")
        self.assertEqual(status(all_pass(), facts=thin, sz={"evidence_quarters": 2})["code"], "BUY")


class ExitRules(unittest.TestCase):
    def roce_fail(self, raw):
        return F("roce", "fail", raw=raw, value=f"{raw}%", threshold="> 25%", label="ROCE")

    def test_two_quarters_below_roce_rule_is_exit(self):
        r = status(all_pass(roce=self.roce_fail(18.0)), prev=all_pass(roce=self.roce_fail(19.5)))
        self.assertEqual((r["code"], r["label"]), ("EXIT", "Exit rule triggered"))
        self.assertEqual(r["speed2"]["status"], "two_quarter_confirmed")

    def test_one_quarter_below_roce_rule_is_trim_warning_not_exit(self):
        r = status(all_pass(roce=self.roce_fail(18.0)), prev=all_pass())
        self.assertEqual(r["code"], "TRIM")
        self.assertEqual(r["speed2"]["status"], "one_quarter_warning")

    def test_same_annual_figure_for_both_quarters_is_a_note_not_a_trigger(self):
        r = status(all_pass(roce=self.roce_fail(18.0)), prev=all_pass(roce=self.roce_fail(18.0)))
        self.assertEqual(r["code"], "HOLD")
        self.assertEqual(r["speed2"]["status"], "clear")
        self.assertTrue(any(x["kind"] == "note" and "annual" in x["text"] for x in r["reasons"]))

    def test_no_earlier_quarter_figure_is_a_note_not_a_trigger(self):
        r = status(all_pass(roce=self.roce_fail(18.0)), prev=[])
        self.assertEqual(r["code"], "HOLD")
        self.assertTrue(any(x["kind"] == "note" for x in r["reasons"]))

    def test_user_switched_rule_off_is_not_an_exit_rule(self):
        off = all_pass(roce=F("roce", "off", raw=18.0))
        self.assertEqual(status(off, prev=off)["code"], "BUY")

    def test_unknown_figure_is_not_judged_and_never_exits(self):
        unk = all_pass(roce=F("roce", "unknown", label="ROCE"))
        r = status(unk, prev=unk)
        self.assertNotEqual(r["code"], "EXIT")
        self.assertIn("ROCE", r["rules"]["unknown"])

    def test_two_negative_sales_quarters_is_exit_one_is_trim(self):
        self.assertEqual(status(all_pass(), facts={**FACTS, "consecutive_quarters_negative_sales_growth": 2})["code"], "EXIT")
        self.assertEqual(status(all_pass(), facts={**FACTS, "consecutive_quarters_negative_sales_growth": 1})["code"], "TRIM")

    def test_margin_compression_needs_two_quarters_to_exit(self):
        hit = {**FACTS, "opm_drop_from_peak_bps": 900}
        self.assertEqual(status(all_pass(), facts=hit, prev_facts=hit)["code"], "EXIT")
        self.assertEqual(status(all_pass(), facts=hit, prev_facts=FACTS)["code"], "TRIM")

    def test_cash_flow_and_debt_rules_use_users_thresholds(self):
        cf = F("cfo_pat", "fail", raw=0.4, value="0.4×", threshold="> 0.6×", label="Operating cash flow ÷ profit")
        cf2 = F("cfo_pat", "fail", raw=0.5, value="0.5×", threshold="> 0.6×", label="Operating cash flow ÷ profit")
        self.assertEqual(status(all_pass(cfo_pat=cf), prev=all_pass(cfo_pat=cf2))["code"], "EXIT")
        de = F("de", "fail", raw=0.9, value="0.90", threshold="< 0.25", label="Debt / equity")
        self.assertEqual(status(all_pass(de=de), prev=all_pass())["code"], "TRIM")

    def test_exact_pledge_at_or_above_limit_is_immediate_exit(self):
        p = F("pledge", "fail", raw=22.0, value="22.0%", threshold="< 10%", limit=10.0)
        r = status(all_pass(pledge=p))
        self.assertEqual(r["code"], "EXIT")
        self.assertEqual(r["speed1"]["status"], "triggered")

    def test_pledged_flag_without_a_percentage_is_not_an_exit(self):
        p = F("pledge", "fail", raw=100.0, value="Pledged", threshold="< 10%", note="only 'pledged / no pledge' is stored, not the exact %")
        self.assertEqual(speed_one([p])["status"], "clear")
        self.assertNotEqual(status(all_pass(pledge=p))["code"], "EXIT")

    def test_exit_beats_trim(self):
        r = status(all_pass(), facts={**FACTS, "consecutive_quarters_negative_sales_growth": 3}, weight_pct=20.0)
        self.assertEqual(r["code"], "EXIT")

    def test_nothing_is_exit_for_a_watchlist_stock(self):
        r = status(all_pass(), facts={**FACTS, "consecutive_quarters_negative_sales_growth": 3}, owned=False)
        self.assertNotEqual(r["code"], "EXIT")


class MissingData(unittest.TestCase):
    def test_too_few_judged_rules_is_not_enough_data_stored_as_watch(self):
        sparse = [F(k, "unknown") for k in KEYS]
        sparse[0] = F("roce", "pass", raw=40.0)  # 1 of 10 judged
        r = status(sparse, prev=[])
        self.assertEqual((r["code"], r["label"], r["enough_data"]), ("WATCH", NO_DATA_LABEL, False))

    def test_banks_exempt_rules_do_not_count_against_the_data_bar(self):
        bank = [F("roe", "pass", raw=20.0), F("pledge", "pass", raw=0.0), F("sales_growth", "pass", raw=40.0), F("opm", "pass", raw=40.0),
                F("promoter", "pass", raw=0.0), F("roa", "pass", raw=1.5), F("equity_assets", "pass", raw=10.0),
                F("de", "unknown", value="n/a"), F("interest_cover", "unknown", value="n/a"), F("current_ratio", "unknown", value="n/a"),
                F("quick_ratio", "unknown", value="n/a"), F("cfo_pat", "unknown", value="n/a")]
        r = status(bank, prev=[])
        self.assertTrue(r["enough_data"])
        self.assertEqual(r["code"], "BUY")

    def test_user_who_switched_most_rules_off_is_judged_on_the_rest(self):
        few = [F("roce", "pass", raw=30.0), F("pledge", "pass", raw=0.0)] + [F(k, "off") for k in KEYS[2:]]
        self.assertTrue(status(few, prev=[])["enough_data"])

    def test_no_rules_on_says_so(self):
        r = status([F(k, "off") for k in KEYS], prev=[])
        self.assertEqual(r["label"], NO_DATA_LABEL)
        self.assertIn("switched on", r["reasons"][0]["text"])

    def test_thin_data_still_exits_on_an_immediate_fact(self):
        p = F("pledge", "fail", raw=30.0, value="30.0%", limit=10.0)
        sparse = [p] + [F(k, "unknown") for k in KEYS[3:]]
        self.assertEqual(status(sparse, prev=[])["code"], "EXIT")


class Wording(unittest.TestCase):
    def test_reasons_never_tell_the_user_what_to_do(self):
        banned = ("you should", "consider", "we suggest", "it is time to", "sell now", "buy now")
        cases = [status(all_pass(), weight_pct=12.0), status(all_pass(), facts={**FACTS, "consecutive_quarters_negative_sales_growth": 2}),
                 status(all_pass()), status(all_pass(), owned=False),
                 status(all_pass(sales_growth=F("sales_growth", "fail", raw=1.0, value="1%", threshold="> 30%", label="Sales growth")))]
        for r in cases:
            text = " ".join(x["text"] for x in r["reasons"]).lower()
            for b in banned:
                self.assertNotIn(b, text)

    def test_every_status_has_at_least_one_reason(self):
        for r in (status(all_pass()), status(all_pass(), weight_pct=12.0), status([F(k, "unknown") for k in KEYS], prev=[])):
            self.assertTrue(r["reasons"])


if __name__ == "__main__":
    unittest.main()
