"""The investment rules behind the Fundamentals scan (ROCE > 25, debt/equity < 0.25 ...) as data instead of constants.

Layers, highest priority first:   this stock (per user)  >  the user's defaults  >  platform defaults  >  built-in.
  * built-in   — DEFS below; what ships with the product and what "reset" ultimately returns to.
  * platform   — set by a platform admin; applies to every user who hasn't set their own.
  * user       — a user's own defaults for all their stocks.
  * stock      — a user's override for one stock.
A row exists only where a value was changed; "reset" just deletes it, so the value falls back to the layer below.
Each rule can also be switched off (it then shows "Off" and is ignored when the scan is scored)."""

DEFS = [
    {"key": "roce", "label": "ROCE", "op": ">", "default": 25.0, "unit": "%", "lo": 0, "hi": 200,
     "note": "return on capital employed — non-financial companies"},
    {"key": "roe", "label": "ROE", "op": ">", "default": 18.0, "unit": "%", "lo": 0, "hi": 200,
     "note": "used instead of ROCE for banks and financials"},
    {"key": "de", "label": "Debt / equity", "op": "<", "default": 0.25, "unit": "×", "lo": 0, "hi": 20,
     "note": "not applied to banks and financials"},
    {"key": "pledge", "label": "Promoter pledge", "op": "<", "default": 10.0, "unit": "%", "lo": 0, "hi": 100,
     "note": "share of the promoters' own holding that is pledged"},
    {"key": "sales_growth", "label": "Sales growth (YoY)", "op": ">", "default": 30.0, "unit": "%", "lo": -50, "hi": 1000,
     "note": "every one of the last 4 quarters must clear it"},
    {"key": "opm", "label": "Operating margin", "op": ">", "default": 30.0, "unit": "%", "lo": -100, "hi": 100,
     "note": "latest quarter"},
    {"key": "interest_cover", "label": "Interest coverage", "op": ">", "default": 10.0, "unit": "×", "lo": 0, "hi": 1000,
     "note": "(profit before tax + interest) ÷ interest, last 4 quarters — not applied to banks and lenders"},
    {"key": "current_ratio", "label": "Current ratio", "op": ">", "default": 1.5, "unit": "×", "lo": 0, "hi": 50,
     "note": "current assets ÷ current liabilities, latest balance sheet — not applied to banks and lenders"},
    {"key": "quick_ratio", "label": "Quick ratio", "op": ">", "default": 1.0, "unit": "×", "lo": 0, "hi": 50,
     "note": "(current assets − inventory) ÷ current liabilities — not applied to banks and lenders"},
    {"key": "cfo_pat", "label": "Operating cash flow ÷ profit", "op": ">", "default": 0.6, "unit": "×", "lo": -5, "hi": 20,
     "note": "operating cash flow ÷ net profit, last 4 quarters (US filings only for now)"},
    {"key": "promoter", "label": "Promoter holding may fall by at most", "op": "≤", "default": 0.5, "unit": "pp", "lo": 0, "hi": 50,
     "note": "over the last 5 quarters, in percentage points"},
]
BY_KEY = {d["key"]: d for d in DEFS}
SCOPES = ("platform", "user", "stock")

# Daily story settings: same layering and storage as the scan rules (framework_overrides), kept in their own list so the Fundamentals
# scan never sees them. `story_stock` is the per-stock switch (no default row = follow the user's scope): 1 = always run, 0 = never.
STORY_DEFS = [
    {"key": "story_scope", "label": "Daily story runs for", "kind": "choice", "default": 2, "lo": 0, "hi": 2,
     "choices": [(0, "No stocks (I run it myself)"), (1, "My positions only"), (2, "Positions and watchlist")],
     "note": "which of your tracked stocks get an AI story automatically each morning"},
    {"key": "story_max", "label": "Stocks per daily run", "kind": "number", "default": 50, "unit": "stocks", "lo": 1, "hi": 500,
     "note": "upper limit, positions first; the rest wait for the next day. Keeps AI usage predictable"},
    {"key": "story_fresh", "label": "Skip a stock analysed within", "kind": "number", "default": 18, "unit": "hours", "lo": 1, "hi": 168,
     "note": "avoids re-running a story that is still fresh"},
]

# Portfolio sizing and discipline: the numbers the story's position-size / exit / add checks and the analyst prompt use. Same layers.
SIZING_DEFS = [
    {"key": "size_max", "label": "Largest a position may grow to", "kind": "number", "default": 8.0, "unit": "% of portfolio", "lo": 1, "hi": 100,
     "note": "above this the story raises a trim alert; also the ceiling for a Core stock"},
    {"key": "size_target", "label": "Trim back toward", "kind": "number", "default": 5.0, "unit": "%", "lo": 0.5, "hi": 100,
     "note": "the weight a trim aims for; also a Core stock's usual size"},
    {"key": "size_watch", "label": "Yellow-flag line", "kind": "number", "default": 5.0, "unit": "%", "lo": 0.5, "hi": 100,
     "note": "between this and the largest size, the story shows a yellow flag"},
    {"key": "size_steady", "label": "A Steady stock's size, up to", "kind": "number", "default": 5.0, "unit": "%", "lo": 0.5, "hi": 100,
     "note": "4+ quarters of accelerating fundamentals"},
    {"key": "size_start", "label": "Starter size for a new Emerging stock", "kind": "number", "default": 2.0, "unit": "%", "lo": 0.1, "hi": 100,
     "note": "first crossed the filters in the last 2 quarters"},
    {"key": "size_confirmed", "label": "Emerging stock after 2 quarters' confirmation", "kind": "number", "default": 3.0, "unit": "%", "lo": 0.1, "hi": 100,
     "note": "what a confirmed Emerging stock may be built to"},
    {"key": "sector_max", "label": "Largest share of the book in one sector", "kind": "number", "default": 30.0, "unit": "%", "lo": 5, "hi": 100,
     "note": "above this the story raises a yellow flag"},
    {"key": "exit_days", "label": "Exit within this long after a confirmed two-quarter deterioration", "kind": "number", "default": 30, "unit": "days", "lo": 1, "hi": 365,
     "note": "holding past it is flagged as a late exit"},
    {"key": "avg_down_max", "label": "Times you may average down", "kind": "number", "default": 1, "unit": "times", "lo": 0, "hi": 10,
     "note": "one more than this is flagged until the first lot is in profit"},
    {"key": "evidence_quarters", "label": "Quarters of evidence before adding to a stock", "kind": "number", "default": 2, "unit": "quarters", "lo": 1, "hi": 8,
     "note": "each of them must clear your sales-growth rule"},
    {"key": "lot_gap", "label": "Flag a second lot in the same quarter as the first", "kind": "choice", "default": 1, "lo": 0, "hi": 1,
     "choices": [(1, "Yes"), (0, "No")], "note": "the framework says build slowly: one lot per quarter"},
]
STORY_BY_KEY = {d["key"]: d for d in STORY_DEFS + SIZING_DEFS}
STORY_STOCK_KEY = "story_stock"


def _blank() -> dict:
    return {d["key"]: {**d, "value": d["default"], "enabled": True, "source": "built-in"} for d in DEFS}


def builtin() -> dict:
    return _blank()


def _apply(out: dict, rows, scope: str) -> None:
    for r in rows:
        if r["scope"] == scope and r["key"] in out:
            out[r["key"]].update(value=r["value"], enabled=bool(r["enabled"]), source=scope)


def _rows(u, user_id, symbol):
    return u.execute(
        "SELECT scope, key, value, enabled FROM framework_overrides WHERE scope = 'platform' "
        "OR (scope = 'user' AND user_id = ?) OR (scope = 'stock' AND user_id = ? AND symbol = ?)",
        (user_id or 0, user_id or 0, symbol or ""),
    ).fetchall()


def resolve(u, user_id: int | None, symbol: str | None = None) -> dict:
    """{key: rule dict with the effective value/enabled and `source` = built-in | platform | user | stock}."""
    rows, out = _rows(u, user_id, symbol), _blank()
    for scope in ("platform", "user") + (("stock",) if symbol else ()):
        _apply(out, rows, scope)
    return out


def layers(u, user_id: int, symbol: str | None = None) -> dict:
    """For the editors: each rule's effective state plus what a reset would fall back to."""
    effective = resolve(u, user_id, symbol)
    parent = resolve(u, user_id, None) if symbol else resolve_platform(u)
    return {k: {**v, "reset_value": parent[k]["value"], "reset_enabled": parent[k]["enabled"], "reset_source": parent[k]["source"],
                "customised": v["source"] == ("stock" if symbol else "user")} for k, v in effective.items()}


def resolve_platform(u) -> dict:
    out = _blank()
    _apply(out, u.execute("SELECT scope, key, value, enabled FROM framework_overrides WHERE scope = 'platform'").fetchall(), "platform")
    return out


def platform_layers(u) -> dict:
    eff = resolve_platform(u)
    return {k: {**v, "reset_value": BY_KEY[k]["default"], "reset_enabled": True, "reset_source": "built-in", "customised": v["source"] == "platform"}
            for k, v in eff.items()}


class RuleError(ValueError):
    pass


def validate(key: str, value) -> float:
    d = BY_KEY.get(key) or STORY_BY_KEY.get(key) or ({"key": key, "label": "Daily story", "lo": 0, "hi": 1} if key == STORY_STOCK_KEY else None)
    if not d:
        raise RuleError("Unknown rule.")
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise RuleError(f"{d['label']}: enter a number.")
    if v != v or not (d["lo"] <= v <= d["hi"]):
        raise RuleError(f"{d['label']}: must be between {d['lo']:g} and {d['hi']:g}.")
    return round(v, 4)


def save(u, scope: str, user_id: int, symbol: str, key: str, value, enabled: bool) -> None:
    if scope not in SCOPES:
        raise RuleError("Unknown scope.")
    v = validate(key, value)
    uid, sym = (0 if scope == "platform" else user_id), (symbol if scope == "stock" else "")
    if key in STORY_BY_KEY:
        base = _story_blank() if scope == "platform" else story_resolve(u, user_id if scope == "stock" else 0, None, platform_only=(scope == "user"))
    elif key == STORY_STOCK_KEY:
        base = {key: {"value": None, "enabled": True}}  # no layer below: "follow my default" is the absence of a row
    else:
        base = _blank() if scope == "platform" else (resolve_platform(u) if scope == "user" else resolve(u, user_id, None))
    # saving exactly what the layer below already says is not a customisation — drop any override instead
    if base[key]["value"] is not None and abs(base[key]["value"] - v) < 1e-9 and bool(enabled) == base[key]["enabled"]:
        u.execute("DELETE FROM framework_overrides WHERE scope=? AND user_id=? AND symbol=? AND key=?", (scope, uid, sym, key))
        return
    u.execute(
        "INSERT INTO framework_overrides (scope, user_id, symbol, key, value, enabled) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(scope, user_id, symbol, key) DO UPDATE SET value=excluded.value, enabled=excluded.enabled, updated_at=CURRENT_TIMESTAMP",
        (scope, uid, sym, key, v, 1 if enabled else 0),
    )


def reset(u, scope: str, user_id: int, symbol: str = "", key: str | None = None) -> int:
    uid, sym = (0 if scope == "platform" else user_id), (symbol if scope == "stock" else "")
    q, args = "DELETE FROM framework_overrides WHERE scope=? AND user_id=? AND symbol=?", [scope, uid, sym]
    if key:
        q, args = q + " AND key=?", args + [key]
    return u.execute(q, args).rowcount


def stocks_with_overrides(u, user_id: int) -> list[dict]:
    return [dict(r) for r in u.execute(
        "SELECT o.symbol, COUNT(*) n, s.name FROM framework_overrides o LEFT JOIN market.stocks s ON s.symbol = o.symbol "
        "WHERE o.scope='stock' AND o.user_id=? GROUP BY o.symbol ORDER BY o.symbol", (user_id,))]


# ---------------------------------------------------------------------------- daily story

def _story_blank() -> dict:
    return {d["key"]: {**d, "value": d["default"], "enabled": True, "source": "built-in"} for d in STORY_DEFS + SIZING_DEFS}


def story_resolve(u, user_id: int | None, symbol: str | None = None, platform_only: bool = False) -> dict:
    """Effective daily-story settings: built-in < platform < the user's own. (`platform_only` = what a user reset falls back to.)"""
    out = _story_blank()
    rows = u.execute("SELECT scope, key, value, enabled FROM framework_overrides WHERE scope = 'platform' "
                     "OR (scope = 'user' AND user_id = ?)", (user_id or 0,)).fetchall()
    for scope in ("platform",) if platform_only else ("platform", "user"):
        _apply(out, rows, scope)
    return out


def story_layers(u, user_id: int) -> dict:
    eff = story_resolve(u, user_id)
    parent = story_resolve(u, user_id, platform_only=True)
    return {k: {**v, "reset_value": parent[k]["value"], "reset_enabled": True, "reset_source": parent[k]["source"], "customised": v["source"] == "user"}
            for k, v in eff.items()}


def story_platform_layers(u) -> dict:
    eff = story_resolve(u, 0, platform_only=True)
    return {k: {**v, "reset_value": STORY_BY_KEY[k]["default"], "reset_enabled": True, "reset_source": "built-in", "customised": v["source"] == "platform"}
            for k, v in eff.items()}


def story_stock_override(u, user_id: int, symbol: str):
    """1 = always run this stock's daily story, 0 = never, None = follow the user's setting."""
    r = u.execute("SELECT value FROM framework_overrides WHERE scope='stock' AND user_id=? AND symbol=? AND key=?", (user_id, symbol, STORY_STOCK_KEY)).fetchone()
    return None if r is None else int(r["value"])


def story_plan(u, user_id: int) -> dict:
    s = story_resolve(u, user_id)
    over = {r["symbol"]: int(r["value"]) for r in u.execute(
        "SELECT symbol, value FROM framework_overrides WHERE scope='stock' AND user_id=? AND key=?", (user_id, STORY_STOCK_KEY))}
    return {"scope": int(s["story_scope"]["value"]), "max": int(s["story_max"]["value"]), "fresh": int(s["story_fresh"]["value"]), "over": over}


def sizing_plan(u, user_id: int) -> dict:
    """The user's effective sizing / discipline numbers: {key: value} (built-in < platform < the user's own)."""
    s = story_resolve(u, user_id)
    return {d["key"]: s[d["key"]]["value"] for d in SIZING_DEFS}


def sizing_defaults() -> dict:
    return {d["key"]: d["default"] for d in SIZING_DEFS}


def story_select(plan: dict, positions) -> list:
    """Tracked stocks (rows with symbol + anchor_type, positions listed before watchlist) whose daily story should run, capped at plan['max']."""
    chosen = []
    for p in positions:
        o = plan["over"].get(p["symbol"])
        if o == 0:
            continue
        if o == 1 or plan["scope"] == 2 or (plan["scope"] == 1 and p["anchor_type"] == "position"):
            chosen.append(p)
    return chosen[: plan["max"]]
