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


def evaluate_framework(q_rows: list, sector: str | None, th: dict | None = None, annual: list | None = None) -> dict:
    """`q_rows`: quarterly (non-annual) quarterly_data rows, oldest first, latest last. `th`: the resolved rules from
    app.framework.resolve() (built-in defaults when omitted). Returns {"filters": [...], "facts": {...}, "bucket_guess",
    "not_available"}. Each filter: {key, label, value, threshold, status: pass|fail|unknown|off, note, ...}. A filter whose data
    we don't hold is 'unknown' (never guessed, never counted against the stock); one the user switched off is 'off'.
    `annual`: optional fiscal-year rows (app.annual.annual_rows), oldest first - the operating-cash-flow rule falls back to the
    latest year that has both cash flow and a positive profit when the quarters carry no cash flow (India reports it yearly)."""
    from app import framework

    th = th or framework.builtin()
    rows = [dict(r) for r in q_rows]
    latest = rows[-1] if rows else {}
    bank = is_bank_or_financial(sector)
    filters: list[dict] = []

    def add(rule, label, value, threshold, status, note=None, raw=None):
        r = th[rule]
        if not r["enabled"]:
            status, note = "off", "switched off for this scan"
        filters.append({"key": rule, "label": label, "value": value, "threshold": threshold, "status": status, "note": note,
                        "rule": rule, "op": r["op"], "limit": r["value"], "unit": r["unit"], "enabled": bool(r["enabled"]), "source": r["source"],
                        "raw": raw})  # raw = the number the status was judged on (None = not computable), so a screen can re-judge it against other thresholds

    # ROCE (ROE for banks/financials)
    rk = "roe" if bank else "roce"
    thr = th[rk]["value"]
    v = _f(latest.get("roe_pct" if bank else "roce_pct"))
    add(rk, "ROE" if bank else "ROCE", None if v is None else f"{v:.1f}%", f"> {thr:g}%",
        "unknown" if v is None else ("pass" if v > thr else "fail"),
        "banks/financials use ROE instead of ROCE" if bank else None, raw=v)
    # Debt / equity
    de = _f(latest.get("debt_equity"))
    de_thr = th["de"]["value"]
    # Banks and lenders are exempt by default (their debt IS the business). An exchange or asset manager is also labelled
    # "Financial Services" yet has a perfectly good D/E, so a rule the user set for THIS stock is applied regardless.
    if bank and th["de"]["source"] != "stock":
        add("de", "Debt / equity", "n/a", f"< {de_thr:g}", "unknown",
            "rule skipped for banks/financials — set your own rule for this stock (gear icon) to apply it" if de is not None else
            "rule does not apply to banks/financials")
    else:
        add("de", "Debt / equity", None if de is None else f"{de:.2f}", f"< {de_thr:g}",
            "unknown" if de is None else ("pass" if de < de_thr else "fail"), raw=de)
    # Promoter pledge — the exact % where we hold it (India shareholding filings), else the pledged / no-pledge flag
    pl_thr = th["pledge"]["value"]
    pct, flag = _f(latest.get("promoter_pledge_pct")), latest.get("promoter_pledge")
    if pct is not None:
        add("pledge", "Promoter pledge", f"{pct:.1f}%", f"< {pl_thr:g}%", "pass" if pct < pl_thr else "fail", raw=pct)
    else:
        add("pledge", "Promoter pledge", flag if flag else None, f"< {pl_thr:g}%", "unknown" if not flag else ("fail" if flag == "Pledged" else "pass"),
            "only 'pledged / no pledge' is stored, not the exact %" if flag == "Pledged" else None,
            raw=(100.0 if flag == "Pledged" else (0.0 if flag else None)))
    # Sales growth, last 4 quarters
    sg = th["sales_growth"]["value"]
    growth = [_f(r.get("revenue_growth_pct")) for r in rows[-4:]]
    known = [g for g in growth if g is not None]
    g_status = "unknown" if not known else ("pass" if all(g > sg for g in known) else "fail")
    add("sales_growth", "Sales growth (YoY, last 4Q)",
        None if not known else " → ".join(f"{g:.0f}%" for g in known), f"> {sg:g}%", g_status,
        None if len(known) == 4 or not known else f"only {len(known)} of 4 quarters have a YoY figure", raw=min(known) if known else None)
    # Operating margin
    om = th["opm"]["value"]
    opm = _f(latest.get("opm_pct"))
    add("opm", "Operating margin", None if opm is None else f"{opm:.1f}%", f"> {om:g}%",
        "unknown" if opm is None else ("pass" if opm > om else "fail"),
        "framework threshold is for high-margin businesses" if om >= 25 else None, raw=opm)
    # Promoter holding: high and not falling by more than the tolerance
    tol = th["promoter"]["value"]
    prom = [_f(r.get("promoter_pct")) for r in rows[-5:]]
    prom_k = [p for p in prom if p is not None]
    if len(prom_k) >= 2:
        chg = prom_k[-1] - prom_k[0]
        add("promoter", "Promoter holding (non-decreasing)", f"{prom_k[-1]:.1f}% ({chg:+.1f} pp)", f"falls ≤ {tol:g} pp",
            "fail" if chg < -tol else "pass", raw=chg)
    else:
        add("promoter", "Promoter holding (non-decreasing)", None if not prom_k else f"{prom_k[-1]:.1f}%", f"falls ≤ {tol:g} pp", "unknown")
    # ---- Coverage, liquidity and cash conversion (inputs stored by the SEC and BSE ingestion; see quarterly_data) ----
    india = str(latest.get("symbol") or "").startswith("NSE:")
    last4 = rows[-4:]

    def lender_exempt(rule):  # banks/lenders have no classified balance sheet and their interest IS the business
        return bank and th[rule]["source"] != "stock"

    def ratio(x):
        return None if x is None else f"{x:.1f}×"

    # Interest coverage = (PBT + interest) / interest, summed over the last 4 quarters (or the latest quarter if that is all we have)
    ic_thr = th["interest_cover"]["value"]
    if lender_exempt("interest_cover"):
        add("interest_cover", "Interest coverage", "n/a", f"> {ic_thr:g}×", "unknown", "rule does not apply to banks/lenders")
    else:
        pairs = [(_f(r.get("pbt_cr")), _f(r.get("finance_cost_cr"))) for r in last4]
        full = len(pairs) == 4 and all(p is not None and f is not None for p, f in pairs)
        use = pairs if full else [pairs[-1]] if pairs and pairs[-1][0] is not None and pairs[-1][1] is not None else []
        if not use:
            add("interest_cover", "Interest coverage", None, f"> {ic_thr:g}×", "unknown", "interest expense isn't reported/stored for this company")
        else:
            pbt, fc = sum(p for p, _ in use), sum(f for _, f in use)
            if fc <= 0:
                add("interest_cover", "Interest coverage", "no interest cost", f"> {ic_thr:g}×", "pass", raw=1e9)
            else:
                cov = (pbt + fc) / fc
                add("interest_cover", "Interest coverage", ratio(cov), f"> {ic_thr:g}×", "pass" if cov > ic_thr else "fail",
                    None if full else "latest quarter only (fewer than 4 quarters of interest data)", raw=cov)

    # Current / quick ratio from the most recent balance sheet we hold (India: latest fiscal year-end; US: every quarter)
    bs = next((r for r in reversed(rows[-8:]) if _f(r.get("current_assets_cr")) is not None and _f(r.get("current_liabilities_cr"))), None)
    for rule, label, qk in (("current_ratio", "Current ratio", False), ("quick_ratio", "Quick ratio", True)):
        thr_v = th[rule]["value"]
        if lender_exempt(rule):
            add(rule, label, "n/a", f"> {thr_v:g}×", "unknown", "rule does not apply to banks/lenders")
        elif not bs:
            add(rule, label, None, f"> {thr_v:g}×", "unknown", "no balance sheet with current assets/liabilities for this company")
        else:
            ca, cl, inv = _f(bs["current_assets_cr"]), _f(bs["current_liabilities_cr"]), _f(bs.get("inventory_cr"))
            val = (ca - (inv or 0)) / cl if qk else ca / cl
            note = f"balance sheet as of {bs['period_end_date']}" + ("; no inventory reported, treated as nil" if qk and inv is None else "")
            add(rule, label, ratio(val), f"> {thr_v:g}×", "pass" if val > thr_v else "fail", note, raw=val)

    # Operating cash flow / net profit over the last 4 quarters
    cf_thr = th["cfo_pat"]["value"]
    cf = [_f(r.get("cfo_cr")) for r in last4]
    npf = [_f(r.get("net_profit_cr")) for r in last4]
    if lender_exempt("cfo_pat"):  # a bank's operating cash flow is dominated by loan and deposit flows, not earnings quality
        add("cfo_pat", "Operating cash flow ÷ profit", "n/a", f"> {cf_thr:g}×", "unknown", "not meaningful for banks/lenders")
    elif len(last4) == 4 and all(c is not None for c in cf) and all(n is not None for n in npf):
        tot_np = sum(npf)
        if tot_np > 0:
            val = sum(cf) / tot_np
            add("cfo_pat", "Operating cash flow ÷ profit", ratio(val), f"> {cf_thr:g}×", "pass" if val > cf_thr else "fail", raw=val)
        else:
            add("cfo_pat", "Operating cash flow ÷ profit", None, f"> {cf_thr:g}×", "unknown", "net profit over the last 4 quarters isn't positive")
    elif (fy := next((a for a in reversed(annual or []) if _f(a.get("cfo_cr")) is not None and _f(a.get("net_profit_cr")) is not None), None)) is not None:
        np_fy = _f(fy.get("net_profit_cr"))
        if np_fy > 0:
            val = _f(fy.get("cfo_cr")) / np_fy
            add("cfo_pat", "Operating cash flow ÷ profit", ratio(val), f"> {cf_thr:g}×", "pass" if val > cf_thr else "fail",
                f"fiscal year ended {fy['period_end_date']} (annual; cash flow isn't reported quarterly)", raw=val)
        else:
            add("cfo_pat", "Operating cash flow ÷ profit", None, f"> {cf_thr:g}×", "unknown", f"net profit for the year ended {fy['period_end_date']} isn't positive")
    else:
        add("cfo_pat", "Operating cash flow ÷ profit", None, f"> {cf_thr:g}×", "unknown",
            "no annual operating cash flow stored for this company yet" if india else "operating cash flow isn't reported for 4 quarters")

    not_available: list[str] = []

    evaluated = [f for f in filters if f["status"] not in ("unknown", "off")]
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
