"""The hard-filter checklist from the user's stock-investment-analysis framework,
computed directly from quarterly_data — deliberately LLM-free. The numbers already live
in the DB; an LLM call should narrate/interpret them, not re-derive them from scratch
(and definitely not hallucinate a plausible-looking ROCE).

Confirmed by reading app/db.py's schema directly before writing this: quarterly_data has
roce_pct/roe_pct, debt_equity, promoter_pct, promoter_pledge (TEXT — "No pledge" or
"Pledged", confirmed live against real data; NOT a numeric percentage, so the framework's
exact ">10%" threshold can only be checked as presence/absence here, not a precise cutoff),
revenue_growth_pct, opm_pct. There is NO interest coverage, current ratio, quick ratio, or
CFO/PAT column anywhere in this schema — those hard filters are explicitly reported as
"not_computable" below rather than guessed at. Closing that gap is a future ingestion
project, out of scope here."""

NOT_COMPUTABLE = ["interest_coverage", "current_ratio", "quick_ratio", "cfo_pat_ratio"]


def is_bank_or_financial(sector: str | None) -> bool:
    if not sector:
        return False
    s = sector.lower()
    return "bank" in s or "financ" in s


def compute_checklist(quarterly_row, sector: str | None) -> dict:
    """`quarterly_row` is a quarterly_data Row (or dict) for one symbol's latest period.
    Returns individual pass/fail booleans (None where the underlying value itself is
    missing — never guessed), a mechanical first-pass bucket guess, and the explicit
    not_computable list so the UI/narrative can be honest about the gap rather than
    silently omitting those criteria."""
    row = dict(quarterly_row)
    bank = is_bank_or_financial(sector)

    def _check(value, cmp_fn):
        return None if value is None else cmp_fn(value)

    roce_or_roe = row.get("roe_pct") if bank else row.get("roce_pct")
    roce_pass = _check(roce_or_roe, lambda v: v > 25)
    de_pass = None if bank else _check(row.get("debt_equity"), lambda v: v < 0.25)
    pledge_pass = None if row.get("promoter_pledge") is None else row["promoter_pledge"] != "Pledged"
    sales_growth_pass = _check(row.get("revenue_growth_pct"), lambda v: v > 30)
    opm_pass = _check(row.get("opm_pct"), lambda v: v > 30)

    checks = {
        "roce_or_roe_pass": roce_pass,
        "debt_equity_pass": de_pass,
        "promoter_pledge_pass": pledge_pass,
        "sales_growth_pass": sales_growth_pass,
        "opm_pass": opm_pass,
    }

    # Mechanical bucket guess: a real pass/fail count over only the criteria we could
    # actually evaluate — never penalizes a stock for data we don't have. The LLM call
    # downstream can override this with qualitative judgment (sector tailwind, moat,
    # etc.) per the framework's own multi-framework triangulation step; this is just the
    # numeric starting point.
    evaluated = [v for v in checks.values() if v is not None]
    passed = sum(1 for v in evaluated if v)
    if not evaluated:
        bucket_guess = "Reserve"
    elif passed == len(evaluated):
        bucket_guess = "Emerging"  # all available hard filters clear — still needs 2Q evidence to size up further
    elif passed >= len(evaluated) - 1:
        bucket_guess = "Reserve"
    else:
        bucket_guess = "Trim-Exit"

    return {
        **checks,
        "is_bank_or_financial": bank,
        "bucket_guess": bucket_guess,
        "not_computable": NOT_COMPUTABLE,
    }


# ----------------------------------------------------------------------------------------------
# Full framework evaluation for the daily per-stock analysis (stock-investment-analysis skill).
# Everything here is computed from stored data — the model narrates and judges, it never calculates.
# ----------------------------------------------------------------------------------------------

def _f(v):
    return None if v is None else float(v)


def evaluate_framework(q_rows: list, sector: str | None) -> dict:
    """`q_rows`: quarterly (non-annual) quarterly_data rows, oldest first, latest last.
    Returns {"filters": [...], "facts": {...}, "bucket_guess": str, "not_available": [...]}.
    Each filter: {key, label, value, threshold, status: pass|fail|unknown, note}. A filter whose data we don't hold is
    'unknown' — never guessed and never counted against the stock."""
    rows = [dict(r) for r in q_rows]
    latest = rows[-1] if rows else {}
    bank = is_bank_or_financial(sector)
    filters: list[dict] = []

    def add(key, label, value, threshold, status, note=None):
        filters.append({"key": key, "label": label, "value": value, "threshold": threshold, "status": status, "note": note})

    # ROCE (ROE for banks/financials)
    v = _f(latest.get("roe_pct" if bank else "roce_pct"))
    thr = 18.0 if bank else 25.0
    add("roce", "ROE" if bank else "ROCE", None if v is None else f"{v:.1f}%", f"> {thr:g}%",
        "unknown" if v is None else ("pass" if v > thr else "fail"),
        "banks/financials use ROE instead of ROCE" if bank else None)
    # Debt / equity
    de = _f(latest.get("debt_equity"))
    if bank:
        add("de", "Debt / equity", "n/a", "< 0.25", "unknown", "rule does not apply to banks/financials")
    else:
        add("de", "Debt / equity", None if de is None else f"{de:.2f}", "< 0.25",
            "unknown" if de is None else ("pass" if de < 0.25 else "fail"))
    # Promoter pledge
    pledge = latest.get("promoter_pledge")
    add("pledge", "Promoter pledge", pledge if pledge else None, "< 10%",
        "unknown" if not pledge else ("fail" if pledge == "Pledged" else "pass"),
        "only 'pledged / no pledge' is stored, not the exact %" if pledge == "Pledged" else None)
    # Sales growth, last 4 quarters
    growth = [_f(r.get("revenue_growth_pct")) for r in rows[-4:]]
    known = [g for g in growth if g is not None]
    g_status = "unknown" if not known else ("pass" if all(g > 30 for g in known) else "fail")
    add("sales_growth", "Sales growth (YoY, last 4Q)",
        None if not known else " → ".join(f"{g:.0f}%" for g in known), "> 30%", g_status,
        None if len(known) == 4 or not known else f"only {len(known)} of 4 quarters have a YoY figure")
    # Operating margin
    opm = _f(latest.get("opm_pct"))
    add("opm", "Operating margin", None if opm is None else f"{opm:.1f}%", "> 30%",
        "unknown" if opm is None else ("pass" if opm > 30 else "fail"), "framework threshold is for high-margin businesses")
    # Promoter holding: high and non-decreasing
    prom = [_f(r.get("promoter_pct")) for r in rows[-5:]]
    prom_k = [p for p in prom if p is not None]
    if len(prom_k) >= 2:
        chg = prom_k[-1] - prom_k[0]
        add("promoter", "Promoter holding (non-decreasing)", f"{prom_k[-1]:.1f}% ({chg:+.1f} pp)", "not falling",
            "fail" if chg < -0.5 else "pass")
    else:
        add("promoter", "Promoter holding (non-decreasing)", None if not prom_k else f"{prom_k[-1]:.1f}%", "not falling", "unknown")
    # Not stored anywhere — said out loud rather than skipped
    not_available = ["Interest coverage (> 10)", "Current ratio (> 1.5)", "Quick ratio (> 1)", "CFO / PAT (> 0.6)"]
    for label in not_available:
        add(label.split(" (")[0].lower().replace(" ", "_").replace("/", ""), label.split(" (")[0], None, label.split("(")[1].rstrip(")"),
            "unknown", "not in our data yet")

    evaluated = [f for f in filters if f["status"] != "unknown"]
    passed = sum(1 for f in evaluated if f["status"] == "pass")
    if not evaluated:
        guess = "Reserve"
    elif passed == len(evaluated):
        guess = "Emerging"
    elif passed >= len(evaluated) - 1:
        guess = "Reserve"
    else:
        guess = "Trim-Exit"

    # Facts for the three-speed exit check (counts of consecutive bad quarters etc.)
    def trailing_run(values, bad):
        n = 0
        for v in reversed(values):
            if v is None:
                break
            if bad(v):
                n += 1
            else:
                break
        return n

    roce_series = [_f(r.get("roe_pct" if bank else "roce_pct")) for r in rows[-4:]]
    opm_series = [_f(r.get("opm_pct")) for r in rows[-8:]]
    opm_known = [x for x in opm_series if x is not None]
    g_series = [_f(r.get("revenue_growth_pct")) for r in rows[-6:]]
    facts = {
        "growth_last_4q": growth,
        "growth_accelerating": len(known) >= 3 and all(b > a for a, b in zip(known, known[1:])),
        "consecutive_quarters_roce_below_threshold": trailing_run(roce_series, lambda x: x < thr),
        "consecutive_quarters_negative_sales_growth": trailing_run(g_series, lambda x: x < 0),
        "opm_peak_last_8q": max(opm_known) if opm_known else None,
        "opm_latest": opm_known[-1] if opm_known else None,
        "opm_drop_from_peak_bps": round((max(opm_known) - opm_known[-1]) * 100) if opm_known else None,
        "debt_equity_latest": de,
        "promoter_pct_series": prom,
    }
    return {"filters": filters, "facts": facts, "bucket_guess": guess, "not_available": not_available}
