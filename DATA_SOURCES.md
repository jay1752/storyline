# Stocktale — Data Sources

What data the platform serves, where each piece comes from, which source wins when two disagree, how fresh it is, and what is still missing.
Last updated 2026-10-10. Companion to `STATUS.md` (architecture and screens).

**How to read the tables**

| Tag | Meaning |
|---|---|
| **Primary** | The source of record for that field. Shown in the app. |
| **Gap-fill** | Used only where the primary has no value (older years, missing quarters). |
| **Fallback** | Tried when the primary fails or doesn't cover the stock. |
| **Reference** | Shown for context (consensus, profile). Not mixed into reported numbers. |
| **Not available** | No source today. The cell stays blank rather than guessed. |

---

## 1. Where data lives

| Store | File (under `data/`) | Holds | Written by |
|---|---|---|---|
| Market | `market.db` (~6 GB) | `stocks`, `quarterly_data`, `annual_data`, `price_history`, `institutional_holdings`, `macro_context`, `ingestion_runs`, `excluded_stocks`, plus audit tables `quarterly_data_original`, `holdings_original`, `ia_basis_status` | Ingestion jobs + the `apply_indianapi_*` scripts |
| Users | `users.db` | accounts, sessions, tracked positions, snapshots, settings, encrypted AI keys, community, rule overrides, job settings | The app. **Never overwritten by data syncs.** |
| Funds | `funds.db` | ETFs and index funds: facts, holdings, sectors, NAV/price history | `scripts/run_funds.py` |
| IndianAPI staging | `indianapi.db` (~250 MB) | Everything pulled from IndianAPI in queryable form (`ia_fact`, `ia_holding`, `ia_estimate`, `ia_target`, `ia_profile`, `ia_stmt`, `ia_peer` …) | `scripts/load_indianapi.py` |
| IndianAPI raw | `indianapi/raw/<endpoint>/<TICKER>.json` (~230 MB) | Verbatim API responses, `calls.json` call counter | `scripts/indianapi_backfill.py` |

SQLite runs with a rollback journal (not WAL) and **one writer at a time**: run every writing script inside the app container, never from the host while the container is writing.

`data/` is git-ignored. Only code and this document are versioned.

---

## 2. Precedence rules (who wins)

1. **India quarterly results (revenue, operating profit, other income, finance cost, PBT, tax, net profit, EPS):** IndianAPI where it has the quarter and passed the basis gate; otherwise BSE, then NSE filings. Originals of every replaced row are archived in `quarterly_data_original`.
2. **Basis gate** (`scripts/apply_indianapi_gate.py`): per stock, IndianAPI net profit must agree with the BSE filing (within 2%) in at least 80% of overlapping quarters (minimum 3). Net profit is the test because it has no definitional wiggle room — a mismatch means IndianAPI used a different basis (usually standalone vs consolidated). Failing stocks (127) are reverted to BSE/NSE; stocks with fewer than 3 overlapping quarters (about 100) stay on IndianAPI and are flagged `unverified` in `ia_basis_status`.
3. **Banks and NBFCs:** only net profit, PBT, tax % and EPS come from IndianAPI. Its bank "Revenue" and "Financing Profit" are not our interest earned and pre-provision profit, so those stay as filed.
4. **India annual figures:** `annual_data` (IndianAPI, up to ~20 fiscal years) wins per year and per field; older MoneyControl annual rows (back to FY1997 for some stocks; depth varies) fill years and fields IndianAPI lacks (for banks: revenue, operating profit).
5. **Shareholding history (promoter, FII, DII %):** IndianAPI where it has the quarter, otherwise BSE. **Promoter pledge always comes from BSE** (IndianAPI has none).
6. **Balance-sheet detail** (current assets/liabilities, inventory, debt, cash, book value, ROE/ROCE/D-E on the latest quarter): BSE yearly filing. IndianAPI's annual balance sheet is shown separately in the annual table.
7. **US fundamentals:** SEC EDGAR only. IndianAPI is India-only.
8. **Analyst targets, estimates, recommendations:** IndianAPI, shown in their own panel, labelled consensus. Analyst-basis EPS (excludes one-offs) is never mixed into filed EPS rows.
9. **Re-ingestion protection:** BSE/MoneyControl upserts do not overwrite the P&L columns of rows whose `source='indianapi'`; MoneyControl's upsert only ever touches rows it wrote itself.

---

## 3. Data map

### 3.1 Universe, identity, classification

| Data | Source | Role | Notes |
|---|---|---|---|
| India stock list (~5,200) | BSE scrip list + NSE `EQUITY_L.csv` | Primary | `sync_universe` in `bse_source.py`; delisted/defunct names removed by `scripts/clean_delisted.py` and held in `excluded_stocks` |
| US stock list (~10,000) | SEC `company_tickers` | Primary | `sync_universe` in `sec_edgar_source.py` |
| ISIN (India, ~4,800 stocks) | NSE `EQUITY_L.csv` + AMFI market-cap list | Primary | `scripts/fill_isin.py`; the key for matching broker statements |
| Sector (India) | BSE company info | Primary | |
| Sector (US) | SEC SIC code; Nasdaq screener where still unclassified | Primary / Gap-fill | |
| Cap tier (India) | AMFI market-cap list (`app/data/cap_tiers.json`) | Primary | 82 India stocks still have no tier |
| Cap tier (US) | Nasdaq public screener | Primary | `scripts/fill_us_cap_tiers.py`; large ≥ $10B, mid ≥ $2B, small ≥ $300M |

### 3.2 Prices

| Data | Source | Role | Refresh |
|---|---|---|---|
| India daily prices | NSE bhavcopy + BSE bhavcopy (matched by ISIN); split detection via NSE prev-close | Primary | Job `daily_prices_in`, Mon–Fri 19:00 IST |
| India price history backfill | NSE bhavcopy archive (back to 1999), Yahoo Finance | Gap-fill | Manual |
| US daily prices | Yahoo Finance via `yfinance`, batches of 100, stalest first; tracked stocks first | Primary | Job `daily_prices_us`, Tue–Sat 05:30 IST |
| US price history backfill | Tiingo (free tier ~50 requests/hour) | Gap-fill | Mac launchd job only |
| Price for a stock with none stored | `yfinance` (`.NS`, then `.BO`; plain ticker for US), Tiingo for US | Fallback | On demand when a position is proposed or confirmed, and in the chat price tool; time-boxed to 25 s |
| India OHLC via brokers | 5paisa Vendor API, Kite Connect | Fallback | Optional; need daily interactive login; not configured on the VM |

### 3.3 India fundamentals

| Data | Source | Role | Notes |
|---|---|---|---|
| Quarterly P&L, last ~6–8 quarters | BSE Integrated Finance XBRL (`bse`) | Primary for stocks IndianAPI doesn't cover; basis-gate fallback | BSE's filing list is capped at ~20 entries, so history is short |
| Quarterly P&L, 13 quarters | IndianAPI `/historical_stats` | **Primary** where verified | Whole-crore rounding on large companies; OPM recomputed from sales and operating profit |
| Quarters BSE lacks | NSE integrated filings (`nse_integrated`) | Gap-fill | Only inserts periods that don't exist; ~107 symbols |
| YoY revenue growth | NSE results archive (`nse_results`) | Gap-fill | Verified against BSE revenue before use |
| Annual P&L, balance sheet, cash flow, ratios (~20 years) | IndianAPI `/historical_stats` → `annual_data` | **Primary** | Cash flow: operating, investing, financing, free, CFO÷profit |
| Annual P&L, older years (back to FY1997 for some stocks) | MoneyControl annual history (`moneycontrol`) | Gap-fill | Undocumented JSON backend, no official API |
| Operating cash flow (FY2025 onwards) | BSE yearly filing (`CashFlowsFromUsedInOperatingActivities`) | Cross-check | Agrees with IndianAPI in ~93–95% of company-years |
| Finance cost (last 5 quarters) | BSE filings (`bse_ratios`) | Gap-fill | Skips IndianAPI-sourced rows |
| Current assets, current liabilities, inventory | BSE yearly filing (latest year, `bse_ratios`) | **Primary** | Feeds current/quick ratio rules |
| Promoter, FII, DII % (history) | IndianAPI | **Primary** | Promoters agreed 99% with BSE where both exist, FII/DII ~88% |
| Promoter, FII, DII % (latest) and **pledge** | BSE shareholding pattern (`bse_shareholding`) | Primary for pledge; Gap-fill otherwise | Free and public |
| Debtor / inventory / payable days, ROCE (annual) | IndianAPI | **Primary** | |
| Compounded growth (10Y/5Y/3Y/TTM) and ROE summary | IndianAPI | Reference | Company panel |
| Guidance (beat / inline / miss) | MoneyControl `hits_misses` | Primary | Enrichment only |

### 3.4 US fundamentals and holdings

| Data | Source | Role | Notes |
|---|---|---|---|
| Quarterly and annual statements (revenue, operating profit, net profit, EPS, balance sheet, interest, current assets, inventory) | SEC EDGAR `companyfacts` | **Primary** | Free, official; fallback tag lists per field |
| Operating cash flow (quarterly) | SEC EDGAR, derived by differencing year-to-date figures | **Primary** | |
| Institutional holdings | SEC Form 13F bulk data set | Primary | Aggregated to one row per symbol |
| EPS surprise (guidance vs delivered) | Finnhub free tier (4 quarters) | Primary | Enrichment only |
| Company news (feeds the daily analysis) | Finnhub | Reference | India stocks via their US ADR ticker where one exists |

### 3.5 Analyst consensus and company reference (IndianAPI, priority stocks only: Nifty 500 + tracked positions)

| Data | IndianAPI endpoint | Role | Coverage |
|---|---|---|---|
| Price target (mean, high, low, median, analyst count, 90-day trend) | `/stock_target_price` | Reference | 431 with a mean; ~68 priority stocks have no analyst coverage |
| Rating split (Strong Buy to Strong Sell) | `/stock` (`recosBar`) | Reference | |
| Estimates: EPS, revenue, net profit (annual, FY27–FY29), EPS (quarterly) | `/stock_forecasts` | Reference | Sales and net profit arrive in ₹ millions and are shown in ₹ Cr |
| Reported vs consensus EPS (surprise) | `/stock_forecasts` | Reference | Analyst basis: excludes one-offs, differs from filed EPS by up to ~10% |
| Company profile, officers, peers, key metrics, corporate actions, news, moving averages | `/stock` | Reference | Shown in the Company section of the stock page |
| Refinitiv-style statement set (INR millions) | `/stock` (`financials`) | Staged only | Not displayed |

### 3.6 Funds and ETFs (`funds.db`)

| Data | Source | Role |
|---|---|---|
| India index funds (NAV, category, ISIN) | AMFI `NAVAll` + `mfapi.in` (Direct + Growth plans only) | Primary |
| India ETFs | NSE ETF list / API | Primary |
| Tracked index of a fund | Parsed from the fund name; constituents from NSE index CSVs (`nsearchives.nseindia.com/content/indices`) | Primary (names only, unweighted) |
| US ETFs (list) | Nasdaq ETF screener | Primary |
| US ETF facts (expense ratio, AUM, holdings, sectors) | Yahoo via `yfinance` (`funds_data`) | Primary; ~69% of sampled ETFs have holdings |
| India fund expense ratio and AUM | — | **Not available** |

### 3.7 Macro, FX, news

| Data | Source | Role |
|---|---|---|
| US macro (fed funds, CPI, unemployment, GDP) | FRED (free key) | Primary; one row per quarter |
| India macro (CPI inflation, GDP growth) | World Bank | Primary; annual only; no policy rate |
| USD/INR for the combined portfolio view and position weights | Frankfurter (ECB reference rate), then open.er-api.com, then Yahoo `INR=X`; stored in `fx_rates` | Primary; refreshed hourly by the `fx_usd_inr` job (Admin > Scheduled jobs, interval editable); a quote more than 8% from the last one is rejected |
| Market news for the Community briefing and daily analysis | Finnhub | Reference |

### 3.8 User data (never part of syncs)

Tracked positions, anchors, snapshots, rules overrides, profile, community posts, import staging, and the user's own AI provider keys (Fernet-encrypted with `APP_MASTER_KEY`) live in `users.db`. Broker statements are parsed in-app (ISIN-first matching, price-consistency check against the trade-date close). The AI providers are bring-your-own-key; no market data comes from them.

### 3.9 Discovery (derived, no external source)

`stock_metrics` (market.db) holds one row per stock with fundamentals (11,047): the number behind each Investment rule plus P/E, 3-year sales CAGR, 3-year average ROCE, dividend payout and cash conversion. It is computed by `app.discovery.build_metrics()` with the same `evaluate_framework` the story page uses (verified identical on 400 stocks, default and custom thresholds), takes about 30 seconds, and rebuilds itself in the background when older than 3 days. The Analytics landing screens ("Passes your rules", Steady compounders, Fast growers, Debt-free, Cash-rich, Dividend payers, Big and stable, Cheap vs own history) judge these numbers against the signed-in user's own thresholds at request time.

---

## 4. Refresh

**Scheduled in-app** (`app/scheduler.py`, times editable in Admin → Scheduled jobs): India prices, US prices, Community briefing 07:30 IST, daily stock analysis 08:30 IST.

**Manual** (Admin → Data ingestion, or scripts): all fundamentals sources (`bse`, `sec_edgar`, `nse_integrated`, `nse_results`, `bse_ratios`, `bse_shareholding`, `moneycontrol`, `sec13f`, `finnhub`, `fred`, `worldbank`), funds, and IndianAPI. There is no fundamentals schedule yet.

**IndianAPI refresh plan:** the Developer plan (₹799/month, 10,000 calls, 1 request/second) was used once for the backfill (~9,400 calls). After cancelling it, the free plan allows 500 calls/month: re-pull priority stocks (`/historical_stats`, 1 call each) once per results season; BSE/NSE filings carry the latest quarter in between. The key is in `.env` as `INDIANAPI_KEY`; use host `stock.indianapi.in` (the `dev.` host returned errors and then 403).

**IndianAPI load order** (all inside the app container, from `/app`, `PYTHONPATH=/app`):

```
scripts/indianapi_backfill.py priority-core | priority-addons | rest-core --schedule 50x5,100x2,200x0 | extras
scripts/load_indianapi.py                      # raw JSON -> data/indianapi.db (identity-checked)
scripts/apply_indianapi_quarterly.py --apply   # quarters -> quarterly_data (originals archived)
scripts/apply_indianapi_annual.py              # annual statements + cash flow -> annual_data
scripts/apply_indianapi_gate.py --apply        # net-profit basis gate; reverts 127 stocks
scripts/apply_indianapi_holdings.py --apply    # promoter / FII / DII history
scripts/market_delta.py export | import        # ship repaired India rows to another machine (8 MB) instead of the 6 GB DB
```

Mac is the development copy; the VM (https://stocktale.linkpc.net) owns `users.db`. Move India data to the VM with `market_delta`, not by copying `market.db` over it. The IndianAPI staging file is not on the VM yet, so the Analyst view and Company sections do not appear there.

---

## 5. Measured quality (validated 2026-10-09/10)

| Check | Result |
|---|---|
| IndianAPI vs BSE filings, 62 stocks | net profit (quarterly) 93–97%, annual operating cash flow 93–95%, annual net profit 93%, total assets 86–98%, revenue 85–89%, EPS 87–91%, investing cash flow 71–78% (definition differs) |
| IndianAPI internal consistency | sales − expenses = operating profit 234/234; assets = liabilities 259/259; operating + investing + financing = net cash flow 252/252 |
| IndianAPI vs Screener (17 stocks) | where net profit agreed with BSE, IndianAPI matched Screener's normalised sales/operating profit; where it didn't, BSE matched Screener |
| Basis gate over 3,053 stocks | 2,832 verified, 98 unverified, 127 reverted |
| Analyst targets vs Yahoo (8 large caps) | within 2.6%, similar analyst counts |
| IndianAPI "reported" EPS vs filed EPS (16 company-years) | 11 within 1.5%; 5 differ 3–11% (analyst basis excludes one-offs) |
| Identity check (IndianAPI vs BSE revenue) | 3.3% of checkable stocks disagree; they are held out |

Two defects in our own pipeline were found by this validation and fixed: (1) BSE inline XBRL marks losses with `sign='-'`, which the parser ignored, so every loss was stored as a profit (0 of 24,459 rows were negative); (2) MoneyControl's annual upsert overwrote March quarter rows (same `(symbol, period_end_date)` key) with full-year figures (~39% of March rows were wrong).

---

## 6. Known gaps

- **Promoter pledge:** BSE only (IndianAPI has none).
- **India operating cash flow before FY2025 from filings:** BSE's list window is two fiscal years; older years come from IndianAPI only.
- **Bank annual revenue and operating profit, FY2025–FY2026:** blank (IndianAPI's bank rows are not comparable; MoneyControl's annual history mostly ends at FY2024).
- **Annual/March-quarter key collision:** the March quarter row and an annual row share `(symbol, period_end_date)` in `quarterly_data`, so ~1,988 older IndianAPI March quarters could not be added. Annual rows now live in `annual_data`, but legacy MoneyControl annual rows remain in `quarterly_data`.
- **IndianAPI has no data for:** ETFs, Bajaj Auto, Nippon Life AMC, HCL Infosystems, Bosch Home Comfort and ~1,150 other mostly small or illiquid names; M&M Financial and J&K Bank are not found by its search; tickers with `&` or `-` resolve unreliably and were skipped or looked up by name with an identity check.
- **Analyst coverage:** ~68 priority stocks have no consensus; quarterly EPS estimates exist for ~270 of 493.
- **IndianAPI EPS and the filings:** IndianAPI EPS is bonus/split-adjusted; BSE EPS is not. Mixed series show a step at the switch-over quarter.
- **FX:** the ECB rate is one fixing per working day, so the hourly job mostly re-confirms it; Yahoo is the only intraday source and is last in the chain.
- **India fund expense ratio / AUM:** not available.
- **US:** debt is null for ~40% of stocks; some data gaps are real ceilings of the free sources.

---

## 7. Licensing — review before distribution to other users

The platform is in a **personal testing phase**. Several sources are free or personal-use only and must be replaced or licensed before data is shown to other users.

| Source | Position |
|---|---|
| SEC EDGAR | Free, official, redistributable (fair-use User-Agent required) |
| BSE / NSE filings and bhavcopy | Public disclosures; redistributing exchange price data commercially needs exchange consent (NSE end-of-day tariff ₹1,00,000/year; vendor-to-client redistribution needs written consent) |
| Yahoo Finance (`yfinance`) | Personal use only; unofficial |
| MoneyControl JSON backend | No official API; undocumented endpoints |
| IndianAPI | Upstream provider not disclosed; payload formats resemble Screener.in (statements) and an LSEG/Refinitiv-style estimates feed. Its terms say free use for any purpose but give no redistribution terms. Treat as **testing only** until the provenance is confirmed in writing |
| Tiingo, Finnhub, FRED, World Bank, Nasdaq screener, AMFI, mfapi.in | Free tiers or public data with their own terms; check before commercial display |
| TrueData, EODHD, Trendlyne | Evaluated, not used: TrueData public display needs exchange and vendor approval; EODHD and Trendlyne personal plans forbid display |
