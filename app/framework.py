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
    d = BY_KEY.get(key)
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
    base = _blank() if scope == "platform" else (resolve_platform(u) if scope == "user" else resolve(u, user_id, None))
    # saving exactly what the layer below already says is not a customisation — drop any override instead
    if abs(base[key]["value"] - v) < 1e-9 and bool(enabled) == base[key]["enabled"]:
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
