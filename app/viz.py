"""The Snapshot view of a stock page: a handful of pictures, each answering one question, built only from data Tenetfolio already holds.

Every builder returns None when its data is missing or too thin, and the template leaves that panel out: a panel is never drawn from guesses or
padded. Charts are inline SVG whose colours come from the app's theme variables (light and dark), so there is no chart library and no client script.

    build(symbol, stock, label_fn)   what is the same for every viewer (cached with the rest of the stock page)
    rules_bars(user_id, symbol)      the viewer's own rules against this stock's figures (never cached: it depends on the user's thresholds)
"""
import json
import math
import statistics

from app.db import get_market_conn, get_users_conn
from app.indianapi_db import get_indianapi_conn

SECTOR_INDEX = {"Information Technology": ("^CNXIT", "Nifty IT")}  # only where the sector name and the index mean the same thing
BENCH = {"IN": ("^NSEI", "Nifty 50"), "US": ("^GSPC", "S&P 500")}


def _pts(ys, x0, x1, y0, y1, vmin, vmax):
    n = max(1, len(ys) - 1)
    return [(x0 + (x1 - x0) * i / n, y1 - (y1 - y0) * ((v - vmin) / (vmax - vmin or 1))) for i, v in enumerate(ys)]


def _path(p):
    return "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in p)


def _num(x):
    return float(x) if x is not None else None


# ------------------------------------------------------------------------------------------------------------------------ growth and margin
def _growth(m, symbol, label_fn, money):
    rows = m.execute("SELECT symbol, period_end_date, quarter_label, revenue_cr, opm_pct, revenue_growth_pct FROM quarterly_data "
                     "WHERE symbol = ? AND quarter_label NOT LIKE '%annual%' AND revenue_cr IS NOT NULL ORDER BY period_end_date DESC LIMIT 12", (symbol,)).fetchall()[::-1]
    if len(rows) < 4:
        return None
    labs = [label_fn(r) for r in rows]
    rev = [r["revenue_cr"] for r in rows]
    yoy = [r["revenue_growth_pct"] for r in rows]
    opm = [r["opm_pct"] for r in rows]
    n = len(rows)
    slot = 600 / n
    bw = slot * 0.62
    top = max(rev) * 1.3 or 1
    bars = ""
    for i, v in enumerate(rev):
        h = max(0, 120 * v / top)
        x = 30 + i * slot + (slot - bw) / 2
        bars += f'<rect x="{x:.1f}" y="{160 - h:.1f}" width="{bw:.1f}" height="{h:.1f}" rx="2" class="vz-bar{" on" if i >= n - 4 else ""}"/>'
        bars += f'<text x="{x + bw / 2:.1f}" y="{160 - h - 5:.1f}" class="vz-lab" text-anchor="middle">{v:,.0f}</text>'
        if yoy[i] is not None:
            bars += f'<text x="{x + bw / 2:.1f}" y="186" class="vz-yoy{" up" if yoy[i] >= 10 else (" dn" if yoy[i] < 0 else "")}" text-anchor="middle">{yoy[i]:+.0f}%</text>'
        bars += f'<text x="{x + bw / 2:.1f}" y="202" class="vz-ax" text-anchor="middle">{labs[i]}</text>'
    pts = [(30 + i * slot + slot / 2, o) for i, o in enumerate(opm) if o is not None]
    line = ""
    if len(pts) >= 3:
        lo, hi = min(o for _, o in pts), max(o for _, o in pts)
        span = (hi - lo) or 1
        pp = [(x, 44 - (o - lo) / span * 26) for x, o in pts]
        line = (f'<path d="{_path(pp)}" class="vz-mline"/>' + "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" class="vz-dot2"/>'
                f'<text x="{x:.1f}" y="{y - 7:.1f}" class="vz-lab sm opm" text-anchor="middle">{o:.0f}</text>' for (x, y), (_, o) in zip(pp, pts)))
    svg = (f'<svg viewBox="0 0 640 210" role="img" aria-label="Revenue by quarter with growth and operating margin">'
           f'<text x="30" y="12" class="vz-ax">Operating margin %</text><text x="610" y="12" class="vz-ax" text-anchor="end">bars: revenue {money}</text>'
           f'<text x="4" y="186" class="vz-ax">YoY</text>{bars}{line}</svg>')
    g = [v for v in yoy[-6:] if v is not None]
    read = None
    if len(g) >= 3:
        read = f"Over the last {len(g)} quarters sales growth stayed between <b>{min(g):.0f}%</b> and <b>{max(g):.0f}%</b>."
        if opm[-1] is not None:
            read += f" Latest operating margin <b>{opm[-1]:.1f}%</b>."
    return {"svg": svg, "read": read}


# ------------------------------------------------------------------------------------------------------------------------ profit vs cash
def _cash(m, symbol, market, money):
    if market == "IN":
        rows = m.execute("SELECT period_end_date d, net_profit_cr np, cfo_cr cf FROM annual_data WHERE symbol = ? AND net_profit_cr IS NOT NULL AND cfo_cr IS NOT NULL "
                         "ORDER BY period_end_date DESC LIMIT 12", (symbol,)).fetchall()[::-1]
    else:
        rows = m.execute("SELECT period_end_date d, net_profit_m np, cfo_m cf FROM us_annual WHERE symbol = ? AND net_profit_m IS NOT NULL AND cfo_m IS NOT NULL "
                         "ORDER BY period_end_date DESC LIMIT 12", (symbol,)).fetchall()[::-1]
    if len(rows) < 3:
        return None
    np_, cf = [r["np"] for r in rows], [r["cf"] for r in rows]
    top = max(max(np_), max(cf), 1) * 1.08
    slot = 610 / len(rows)
    bw = slot * 0.36
    out = ""
    for i, r in enumerate(rows):
        x = 24 + i * slot
        h1, h2 = max(0, 150 * np_[i] / top), max(0, 150 * cf[i] / top)
        out += f'<rect x="{x + slot * 0.12:.1f}" y="{170 - h1:.1f}" width="{bw:.1f}" height="{h1:.1f}" rx="1.5" class="vz-bar np"/>'
        out += f'<rect x="{x + slot * 0.12 + bw + 1:.1f}" y="{170 - h2:.1f}" width="{bw:.1f}" height="{h2:.1f}" rx="1.5" class="vz-bar cf{"" if cf[i] >= np_[i] else " short"}"/>'
        out += f'<text x="{x + slot / 2:.1f}" y="188" class="vz-ax" text-anchor="middle">FY{r["d"][2:4]}</text>'
    svg = f'<svg viewBox="0 0 640 196" role="img" aria-label="Net profit against operating cash flow by year">{out}</svg>'
    win = sum(1 for a, b in zip(np_, cf) if b >= a)
    conv = [b / a for a, b in zip(np_, cf) if a > 0]
    read = f"Cash flow matched or beat profit in <b>{win} of {len(rows)} years</b>"
    if conv:
        read += f"; the weakest year converted <b>{min(conv):.2f}×</b>"
    return {"svg": svg, "read": read + "."}


# ------------------------------------------------------------------------------------------------------------------------ ownership
def _clean_name(n: str) -> str:
    n = " ".join(n.split())
    return n.title() if n.isupper() else n


def _ownership(m, symbol, label_fn):
    rows = m.execute("SELECT symbol, period_end_date, quarter_label, promoter_pct p, fii_pct f, dii_pct d FROM quarterly_data WHERE symbol = ? AND "
                     "quarter_label NOT LIKE '%annual%' AND promoter_pct IS NOT NULL AND fii_pct IS NOT NULL AND dii_pct IS NOT NULL ORDER BY period_end_date DESC LIMIT 12",
                     (symbol,)).fetchall()[::-1]
    area = None
    if len(rows) >= 4:
        pro, fii, dii = [r["p"] for r in rows], [r["f"] for r in rows], [r["d"] for r in rows]
        pub = [max(0.0, 100 - a - b - c) for a, b, c in zip(pro, fii, dii)]
        n = len(rows)
        xs = [8 + 462 * i / (n - 1) for i in range(n)]
        y = lambda v: 190 - 178 * v / 100
        cum, paths, labs = [0.0] * n, "", []
        for cls, name, vals in (("pro", "Promoters", pro), ("fii", "FII", fii), ("dii", "DII", dii), ("pub", "Public & other", pub)):
            top = [c + v for c, v in zip(cum, vals)]
            up = " L".join(f"{x:.1f},{y(t):.1f}" for x, t in zip(xs, top))
            dn = " L".join(f"{x:.1f},{y(c):.1f}" for x, c in reversed(list(zip(xs, cum))))
            paths += f'<path d="M{up} L{dn} Z" class="vz-own {cls}"/>'
            labs.append([y((cum[-1] + top[-1]) / 2) + 4, f"{name} {vals[-1]:.1f}%"])
            cum = top
        ys = [l[0] for l in labs]
        for i in range(1, len(ys)):  # labels go bottom to top: keep 15 px apart
            ys[i] = min(ys[i], ys[i - 1] - 15)
        paths += "".join(f'<text x="480" y="{yy:.1f}" class="vz-lab">{t}</text>' for (_, t), yy in zip(labs, ys))
        paths += f'<text x="8" y="206" class="vz-ax">{label_fn(rows[0])}</text><text x="470" y="206" class="vz-ax" text-anchor="end">{label_fn(rows[-1])}</text>'
        area = {"svg": f'<svg viewBox="0 0 640 212" role="img" aria-label="Shareholding by quarter">{paths}</svg>',
                "read": (f"Promoters moved from <b>{pro[0]:.1f}%</b> to <b>{pro[-1]:.1f}%</b>, FII from <b>{fii[0]:.1f}%</b> to <b>{fii[-1]:.1f}%</b>, "
                         f"DII from <b>{dii[0]:.1f}%</b> to <b>{dii[-1]:.1f}%</b>.")}
    holders = None
    try:
        per = m.execute("SELECT DISTINCT period_end FROM holders WHERE symbol = ? ORDER BY period_end DESC LIMIT 2", (symbol,)).fetchall()
    except Exception:  # noqa: BLE001 - table absent on an old database
        per = []
    if per:
        def load(p):
            out, prom = {}, [0, 0.0]
            for r in m.execute("SELECT holder_name, category, pct FROM holders WHERE symbol = ? AND period_end = ?", (symbol, p)):
                if r["category"] == "Needs check":
                    return None  # the promoter names of this filing did not add up to its own total: show none of it
                if r["category"] == "Promoter group":
                    prom[0] += 1
                    prom[1] += r["pct"] or 0
                    continue
                k = " ".join(r["holder_name"].split()).casefold()
                out[k] = {"name": _clean_name(r["holder_name"]), "category": r["category"], "pct": (out.get(k, {}).get("pct") or 0) + (r["pct"] or 0)}
            return out, prom
        cur = load(per[0]["period_end"])
        prev = load(per[1]["period_end"]) if len(per) > 1 else None
        if cur:
            pubs, prom = cur
            lst = []
            for k, v in sorted(pubs.items(), key=lambda kv: -kv[1]["pct"])[:8]:
                d = None
                if prev:
                    d = round(v["pct"] - prev[0].get(k, {}).get("pct", 0), 2)
                lst.append({**v, "pct": round(v["pct"], 2), "chg": d, "new": bool(prev) and k not in prev[0]})
            holders = {"period": per[0]["period_end"], "promoters": {"n": prom[0], "pct": round(prom[1], 2)} if prom[0] else None, "list": lst,
                       "has_prev": bool(prev)}
    if not area and not holders:
        return None
    return {"area": area, "holders": holders}


# ------------------------------------------------------------------------------------------------------------------------ capital and cash cycle
def _capital(m, symbol):
    rows = m.execute("SELECT period_end_date d, roce_pct r, borrowings_cr b, reserves_cr s FROM annual_data WHERE symbol = ? ORDER BY period_end_date DESC LIMIT 10", (symbol,)).fetchall()[::-1]
    ro = [(r["d"], r["r"]) for r in rows if r["r"] is not None]
    if len(ro) < 3:
        return None
    vals = [v for _, v in ro]
    hi = max(max(vals), 1) * 1.15
    P = _pts(vals, 40, 600, 24, 150, min(0, min(vals)), hi)
    base_y = 150 - 126 * (0 - min(0, min(vals))) / (hi - min(0, min(vals)))
    svg = (f'<svg viewBox="0 0 640 186" role="img" aria-label="Return on capital employed by year"><line x1="40" x2="600" y1="{base_y:.1f}" y2="{base_y:.1f}" class="vz-grid"/>'
           f'<path d="{_path(P)}" class="vz-line"/>' + "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" class="vz-dot"/><text x="{x:.1f}" y="{y - 9:.1f}" class="vz-lab" text-anchor="middle">{v:.0f}%</text>'
                                                             for (x, y), v in zip(P, vals))
           + "".join(f'<text x="{x:.1f}" y="172" class="vz-ax" text-anchor="middle">FY{d[2:4]}</text>' for (x, _), (d, _v) in zip(P, ro)) + "</svg>")
    last = rows[-1]
    debt = None
    if last["b"] is not None and last["s"] and last["s"] > 0:
        debt = {"b": last["b"], "s": last["s"], "pct": min(100.0, last["b"] / last["s"] * 100), "fy": last["d"][2:4]}
    return {"svg": svg, "debt": debt,
            "read": f"ROCE ranged from <b>{min(vals):.0f}%</b> to <b>{max(vals):.0f}%</b> over the last {len(vals)} years."}


def _cycle(m, symbol):
    rows = m.execute("SELECT period_end_date d, debtor_days dd, inventory_days iv, days_payable dp, cash_conversion_cycle cc FROM annual_data WHERE symbol = ? "
                     "ORDER BY period_end_date DESC LIMIT 10", (symbol,)).fetchall()[::-1]
    series = [(nm, cls, [r[k] for r in rows]) for nm, cls, k in (("Payables", "l3", "dp"), ("Inventory", "l1", "iv"), ("Debtors", "l2", "dd"))]
    series = [s for s in series if sum(v is not None for v in s[2]) >= 3]
    if not series or len(rows) < 3:
        return None
    top = max(v for _, _, vs in series for v in vs if v is not None) * 1.1 or 1
    n = len(rows)
    xs = [40 + 520 * i / (n - 1) for i in range(n)]
    out = ""
    for t in (0, round(top / 2), round(top)):
        y = 150 - 130 * t / top
        out += f'<line x1="40" x2="560" y1="{y:.0f}" y2="{y:.0f}" class="vz-grid"/><text x="8" y="{y + 4:.0f}" class="vz-ax">{t}</text>'
    for nm, cls, vs in series:
        pts = [(x, 150 - 130 * v / top) for x, v in zip(xs, vs) if v is not None]
        out += f'<path d="{_path(pts)}" class="vz-cyc {cls}"/><circle cx="{pts[-1][0]:.1f}" cy="{pts[-1][1]:.1f}" r="3.5" class="vz-cd {cls}"/>'
        out += f'<text x="{pts[-1][0] + 8:.1f}" y="{pts[-1][1] + 4:.1f}" class="vz-lab">{nm} {[v for v in vs if v is not None][-1]:.0f}</text>'
    out += "".join(f'<text x="{x:.0f}" y="172" class="vz-ax" text-anchor="middle">FY{r["d"][2:4]}</text>' for x, r in zip(xs, rows))
    cc = [r["cc"] for r in rows if r["cc"] is not None]
    read = f"Cash conversion cycle went from <b>{cc[0]:.0f}</b> to <b>{cc[-1]:.0f} days</b> over {len(cc)} years." if len(cc) >= 3 else None
    return {"svg": f'<svg viewBox="0 0 640 184" role="img" aria-label="Debtor, inventory and payable days by year">{out}</svg>', "read": read}


# ------------------------------------------------------------------------------------------------------------------------ valuation, analysts
def _value(m, symbol, last_price):
    r = m.execute("SELECT pe, pe_low, pe_high FROM stock_metrics WHERE symbol = ?", (symbol,)).fetchone()
    pe = None
    if r and r["pe"] and r["pe_low"] and r["pe_high"] and r["pe_high"] > r["pe_low"] and r["pe"] > 0:
        pe = {"now": round(r["pe"], 1), "lo": round(r["pe_low"], 1), "hi": round(r["pe_high"], 1),
              "pos": max(0.0, min(100.0, (r["pe"] - r["pe_low"]) / (r["pe_high"] - r["pe_low"]) * 100))}
    an = None
    try:
        ia = get_indianapi_conn()
        t = ia.execute("SELECT mean, high, low, n_estimates FROM ia_target WHERE symbol = ?", (symbol,)).fetchone()
        rt = ia.execute("SELECT rating_value, rating_name, n_now, n_1m, n_3m FROM ia_rating WHERE symbol = ? ORDER BY rating_value", (symbol,)).fetchall()
        snaps = {x["age"]: x["mean"] for x in ia.execute("SELECT age, mean FROM ia_target_snapshot WHERE symbol = ?", (symbol,))}
        ia.close()
        target = None
        if t and t["mean"] and t["high"] and t["low"] and t["high"] > t["low"]:
            span = t["high"] - t["low"]
            lo_edge, hi_edge = min(t["low"], last_price or t["low"]), max(t["high"], last_price or t["high"])
            span = (hi_edge - lo_edge) or 1
            pos = lambda v: max(0.0, min(100.0, (v - lo_edge) / span * 100))
            drift = None
            if snaps.get("NinetyDaysAgo"):
                drift = round((t["mean"] / snaps["NinetyDaysAgo"] - 1) * 100, 1)
            target = {"mean": t["mean"], "high": t["high"], "low": t["low"], "n": t["n_estimates"], "lo_edge": lo_edge, "hi_edge": hi_edge,
                      "p_mean": pos(t["mean"]), "p_price": pos(last_price) if last_price else None, "p_lo": pos(t["low"]), "p_hi": pos(t["high"]), "drift": drift}
        ratings = None
        rows = [x for x in rt if x["rating_name"] != "Total"]
        tot = sum(x["n_now"] or 0 for x in rows)
        if tot >= 3:
            tot3 = sum(x["n_3m"] or 0 for x in rows)
            buy = lambda col: sum((x[col] or 0) for x in rows if x["rating_value"] <= 2)
            ratings = {"total": tot, "segments": [{"name": x["rating_name"], "n": x["n_now"] or 0, "pct": (x["n_now"] or 0) / tot * 100, "v": x["rating_value"]} for x in rows if (x["n_now"] or 0) > 0],
                       "buy_now": round(buy("n_now") / tot * 100), "buy_3m": round(buy("n_3m") / tot3 * 100) if tot3 else None}
        if target or ratings:
            an = {"target": target, "ratings": ratings}
    except Exception:  # noqa: BLE001 - the staging file may be absent or older than this view
        an = None
    if not pe and not an:
        return None
    read = None
    if pe:
        read = f"P/E <b>{pe['now']}×</b> sits <b>{pe['pos']:.0f}%</b> of the way up its own 3-year range ({pe['lo']}× to {pe['hi']}×)."
    return {"pe": pe, "analysts": an, "read": read}


# ------------------------------------------------------------------------------------------------------------------------ peers
def _peers(m, symbol, last_price):
    try:
        ia = get_indianapi_conn()
        peers = [dict(r) for r in ia.execute("SELECT peer_name n, pe, pb, market_cap mc FROM ia_peer WHERE symbol = ? AND pe > 0 AND pb > 0 AND market_cap > 0", (symbol,))]
        ia.close()
    except Exception:  # noqa: BLE001
        return None
    if len(peers) < 3:
        return None
    me = m.execute("SELECT pe, mcap FROM stock_metrics WHERE symbol = ?", (symbol,)).fetchone()
    bv = m.execute("SELECT book_value_per_share b FROM quarterly_data WHERE symbol = ? AND book_value_per_share > 0 ORDER BY period_end_date DESC LIMIT 1", (symbol,)).fetchone()
    pts = [{**p, "me": False} for p in peers]
    if me and me["pe"] and me["pe"] > 0 and me["mcap"] and bv and last_price:
        pts.append({"n": "This stock", "pe": me["pe"], "pb": last_price / bv["b"], "mc": me["mcap"], "me": True})
    mpe, mpb = statistics.median(p["pe"] for p in pts), statistics.median(p["pb"] for p in pts)
    shown = [p for p in pts if p["me"] or (p["pe"] <= 3 * mpe and p["pb"] <= 3 * mpb)]
    hidden = len(pts) - len(shown)
    if len(shown) < 3:
        return None
    x0, x1 = min(p["pe"] for p in shown) * 0.9, max(p["pe"] for p in shown) * 1.08
    y1 = max(p["pb"] for p in shown) * 1.15
    X = lambda v: 44 + (v - x0) / (x1 - x0 or 1) * 540
    Y = lambda v: 176 - v / y1 * 156
    out = ""
    for i in range(5):
        v = x0 + (x1 - x0) * i / 4
        out += f'<line x1="{X(v):.0f}" x2="{X(v):.0f}" y1="14" y2="176" class="vz-grid"/><text x="{X(v):.0f}" y="194" class="vz-ax" text-anchor="middle">{v:.0f}×</text>'
    for i in range(4):
        v = y1 * i / 3
        out += f'<line x1="44" x2="584" y1="{Y(v):.0f}" y2="{Y(v):.0f}" class="vz-grid"/><text x="38" y="{Y(v) + 4:.0f}" class="vz-ax" text-anchor="end">{v:.0f}×</text>'
    top_mc = max(p["mc"] for p in shown)
    for p in sorted(shown, key=lambda q: -q["mc"]):
        r = 8 + 24 * math.sqrt(p["mc"] / top_mc)
        nm = p["n"] if len(p["n"]) <= 14 else p["n"].split()[0]
        out += f'<circle cx="{X(p["pe"]):.1f}" cy="{Y(p["pb"]):.1f}" r="{r:.1f}" class="vz-bub{" me" if p["me"] else ""}"><title>{p["n"]}: P/E {p["pe"]:.1f}, P/B {p["pb"]:.1f}</title></circle>'
        out += f'<text x="{X(p["pe"]):.1f}" y="{Y(p["pb"]) + 4:.1f}" class="vz-lab {"b" if p["me"] else "s"}" text-anchor="middle">{nm}</text>'
    out += '<text x="584" y="209" class="vz-ax" text-anchor="end">P/E →</text><text x="44" y="8" class="vz-ax">↑ P/B</text>'
    return {"svg": f'<svg viewBox="0 0 640 214" role="img" aria-label="Peers by P/E and P/B; bubble size is market cap">{out}</svg>', "hidden": hidden, "has_me": any(p["me"] for p in shown)}


# ------------------------------------------------------------------------------------------------------------------------ relative performance
def _relative(m, symbol, market, sector):
    px = {r["date"]: r["close"] for r in m.execute("SELECT date, close FROM price_history WHERE symbol = ? AND date >= date('now','-380 day') ORDER BY date", (symbol,))}
    lines = []
    b_sym, b_name = BENCH.get(market, BENCH["IN"])
    series = [(symbol, "This stock", "me")]
    others = [(b_sym, b_name, "bench")]
    if sector in SECTOR_INDEX and market == "IN":
        others.append((SECTOR_INDEX[sector][0], SECTOR_INDEX[sector][1], "sect"))
    data = {symbol: px}
    for s, _n, _c in others:
        try:
            data[s] = {r["date"]: r["close"] for r in m.execute("SELECT date, close FROM index_prices WHERE symbol = ? AND date >= date('now','-380 day') ORDER BY date", (s,))}
        except Exception:  # noqa: BLE001
            data[s] = {}
    common = sorted(set(px) & set(data[b_sym]))
    common = [d for d in common if d >= sorted(common)[max(0, len(common) - 252)]]
    if len(common) < 100:
        return None
    base = {s: data[s].get(common[0]) for s in data}
    allv, drawn = [], []
    for s, nm, cls in series + others:
        if not base.get(s):
            continue
        ys = [data[s][d] / base[s] * 100 for d in common if d in data[s]]
        if len(ys) < 100:
            continue
        allv += ys
        drawn.append((nm, cls, ys))
    if len(drawn) < 2:
        return None
    lo, hi = min(allv) * 0.97, max(allv) * 1.03
    out = ""
    for t in (lo, (lo + hi) / 2, hi):
        y = 170 - 150 * (t - lo) / (hi - lo)
        out += f'<line x1="36" x2="500" y1="{y:.0f}" y2="{y:.0f}" class="vz-grid"/><text x="32" y="{y + 4:.0f}" class="vz-ax" text-anchor="end">{t:.0f}</text>'
    if lo < 100 < hi:
        y100 = 170 - 150 * (100 - lo) / (hi - lo)
        out += f'<line x1="36" x2="500" y1="{y100:.1f}" y2="{y100:.1f}" class="vz-grid base"/>'
    ends = []
    for nm, cls, ys in drawn:
        P = _pts(ys, 36, 500, 20, 170, lo, hi)
        out += f'<path d="{_path(P)}" class="vz-rel {cls}"/>'
        ends.append([P[-1][1] + 4, f"{nm} {ys[-1] - 100:+.0f}%", cls])
    ends.sort(key=lambda e: e[0])
    for i in range(1, len(ends)):
        ends[i][0] = max(ends[i][0], ends[i - 1][0] + 14)
    out += "".join(f'<text x="508" y="{y:.1f}" class="vz-lab {c}">{t}</text>' for y, t, c in ends)
    out += f'<text x="36" y="192" class="vz-ax">{common[0]}</text><text x="500" y="192" class="vz-ax" text-anchor="end">{common[-1]}</text>'
    me, bench = drawn[0][2][-1] - 100, drawn[1][2][-1] - 100
    return {"svg": f'<svg viewBox="0 0 640 198" role="img" aria-label="Price against its market index, both set to 100 at the start">{out}</svg>',
            "read": f"Over {len(common)} trading days the stock moved <b>{me:+.1f}%</b>; {drawn[1][0]} moved <b>{bench:+.1f}%</b>.", "bench": drawn[1][0]}


# ------------------------------------------------------------------------------------------------------------------------ deals
def _deals(m, symbol):
    try:
        rows = m.execute("SELECT deal_date, kind, client, side, qty, price FROM deals WHERE symbol = ? AND deal_date >= date('now','-120 day') ORDER BY deal_date DESC, qty DESC LIMIT 8",
                         (symbol,)).fetchall()
        span = m.execute("SELECT MIN(deal_date), MAX(deal_date) FROM deals").fetchone()
    except Exception:  # noqa: BLE001
        return None
    if not rows:
        return None
    return {"rows": [{"date": r["deal_date"], "kind": r["kind"], "client": _clean_name(r["client"]), "side": r["side"], "qty": r["qty"], "price": r["price"],
                      "value_cr": (r["qty"] or 0) * (r["price"] or 0) / 1e7} for r in rows], "since": span[0]}


# ------------------------------------------------------------------------------------------------------------------------ assemble
def build(symbol: str, stock, label_fn, last_price=None) -> dict:
    market = stock["market"]
    money = "₹ Cr" if market == "IN" else "$ M"
    out = {"money": money}
    with get_market_conn() as m:
        # banks and lenders: operating cash flow, debt-to-reserves and stock/debtor days do not mean what they mean for other companies
        row = m.execute("SELECT bank FROM stock_metrics WHERE symbol = ?", (symbol,)).fetchone()
        is_bank = bool(row and row["bank"])
        for key, fn in (("growth", lambda: _growth(m, symbol, label_fn, money)), ("cash", lambda: None if is_bank else _cash(m, symbol, market, money)),
                        ("own", lambda: _ownership(m, symbol, label_fn) if market == "IN" else None),
                        ("capital", lambda: _capital(m, symbol) if market == "IN" and not is_bank else None), ("cycle", lambda: _cycle(m, symbol) if market == "IN" and not is_bank else None),
                        ("value", lambda: _value(m, symbol, last_price)), ("peers", lambda: _peers(m, symbol, last_price) if market == "IN" else None),
                        ("rel", lambda: _relative(m, symbol, market, stock["sector"] if "sector" in stock.keys() else None)),
                        ("deals", lambda: _deals(m, symbol) if market == "IN" else None)):
            try:
                out[key] = fn()
            except Exception:  # noqa: BLE001 - one panel failing must never take the page down
                out[key] = None
    out["has"] = any(out.get(k) for k in ("growth", "cash", "own", "capital", "cycle", "value", "peers", "rel", "deals"))
    return out


def rules_bars(user_id: int, symbol: str):
    """The viewer's rules against this stock's figures: [{label, value, limit, ok, pos, tick}] in the rules' own order. Mirrors discovery.judge."""
    from app import framework as fw

    with get_market_conn() as m:
        r = m.execute("SELECT raws, bank FROM stock_metrics WHERE symbol = ?", (symbol,)).fetchone()
    if not r or not r["raws"]:
        return None
    raws, bank = json.loads(r["raws"]), bool(r["bank"])
    with get_users_conn() as u:
        th = fw.resolve(u, user_id, symbol)
    out = []
    for key, rule in th.items():
        if not rule.get("enabled", True) or (key == "roce" and bank) or (key == "roe" and not bank):
            continue
        if rule.get("fin") and not bank:
            continue
        v = raws.get(key)
        if v is None:
            continue
        t, op, unit = rule["value"], rule["op"], rule["unit"]
        if key == "promoter":      # the figure is the change in promoter holding; a fall is what the rule limits
            shown, ok, cmp_v = max(0.0, -v), v >= -t, max(0.0, -v)
            lim = f"fall ≤ {t:g}{unit}"
            scale = max(t * 2, 0.5)
        elif op == ">":
            shown, ok, cmp_v = v, v > t, v
            lim = f"> {t:g}{unit}"
            scale = t * 2.5 if t > 0 else max(abs(v) * 2, 5)
        else:
            shown, ok, cmp_v = v, v < t, v
            lim = f"< {t:g}{unit}"
            scale = t * 2 if t > 0 else max(abs(v) * 2, 1)
        thr = t if key != "promoter" else t
        out.append({"label": rule["label"], "value": f"{shown:,.2f}".rstrip("0").rstrip(".") + unit if abs(shown) < 1000 else f"{shown:,.0f}{unit}", "limit": lim, "ok": ok,
                    "pos": max(0.0, min(100.0, (max(cmp_v, 0) / scale) * 100)), "tick": max(0.0, min(100.0, thr / scale * 100)), "key": key})
    return out or None
