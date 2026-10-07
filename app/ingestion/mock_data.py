"""Canonical mock market data — moved here from seed.py so it's owned by the ingestion
layer (the thing that will eventually be swapped for real 5paisa/SEC EDGAR calls), not
by a one-off dev bootstrap script. seed.py now imports from here for the market-data half
of its job, and keeps only user-specific (tracked_positions) seeding to itself.
"""

QUARTERS = [f"Q{i}" for i in range(1, 9)]

STOCKS = [
    dict(symbol="NSE:TATAMOTORS", exchange="NSE", name="Tata Motors Ltd.", sector="Auto", market="IN"),
    dict(symbol="NSE:INFY", exchange="NSE", name="Infosys Ltd.", sector="IT Services", market="IN"),
    dict(symbol="NSE:HDFCBANK", exchange="NSE", name="HDFC Bank Ltd.", sector="Banking", market="IN"),
    dict(symbol="NASDAQ:NVDA", exchange="NASDAQ", name="NVIDIA Corp.", sector="Semiconductors", market="US"),
    dict(symbol="NASDAQ:MSFT", exchange="NASDAQ", name="Microsoft Corp.", sector="Software", market="US"),
    dict(symbol="NASDAQ:AAPL", exchange="NASDAQ", name="Apple Inc.", sector="Consumer Tech", market="US"),
]

QUARTERLY = {
    "NSE:TATAMOTORS": dict(
        revenue_cr=[10850, 11200, 11640, 12250, 12780, 13350, 13820, 14400],
        operating_profit_cr=[770, 829, 908, 1005, 1086, 1188, 1258, 1354],
        opm_pct=[7.1, 7.4, 7.8, 8.2, 8.5, 8.9, 9.1, 9.4],
        other_income_cr=[120, 110, 135, 140, 150, 145, 160, 170],
        net_profit_cr=[520, 560, 610, 690, 740, 810, 860, 930],
        eps=[1.53, 1.65, 1.79, 2.03, 2.18, 2.38, 2.53, 2.74],
        revenue_growth_pct=[8, 10, 12, 15, 17, 18, 19, 20],
        guidance_vs_delivered=["met", "met", "beat", "met", "beat", "beat", "beat", "beat"],
        growth_vs_sector_pp=[1, 2, 3, 5, 7, 7, 9, 9],
        margin_vs_sector_pp=[0.3, 0.2, 0.6, 0.9, 1.2, 1.6, 1.8, 2.1],
        fii_pct=[10.8, 11.1, 11.4, 11.7, 12.1, 12.4, 12.9, 13.2],
        dii_pct=[12.5, 12.3, 12.6, 12.9, 12.9, 13.0, 13.1, 13.2],
        promoter_pct=[41.2] * 8,
        promoter_pledge=["No pledge"] * 8,
        sector_momentum=["neutral", "neutral", "favor", "favor", "favor", "neutral", "favor", "favor"],
    ),
    "NSE:INFY": dict(
        revenue_cr=[38500, 39200, 39800, 40600, 41300, 42000, 42800, 43700],
        operating_profit_cr=[8085, 8310, 8557, 8648, 8921, 9156, 9416, 9658],
        opm_pct=[21.0, 21.2, 21.5, 21.3, 21.6, 21.8, 22.0, 22.1],
        other_income_cr=[900, 880, 920, 910, 940, 950, 960, 970],
        net_profit_cr=[6100, 6250, 6400, 6550, 6700, 6850, 7050, 7250],
        eps=[14.7, 15.0, 15.4, 15.7, 16.1, 16.5, 17.0, 17.5],
        revenue_growth_pct=[4, 5, 6, 7, 8, 8, 9, 10],
        guidance_vs_delivered=["met", "met", "met", "beat", "met", "met", "beat", "met"],
        growth_vs_sector_pp=[0, 1, 1, 2, 2, 1, 2, 3],
        margin_vs_sector_pp=[0.5, 0.4, 0.6, 0.5, 0.7, 0.8, 0.9, 1.0],
        fii_pct=[15.1, 15.0, 15.3, 15.2, 15.4, 15.6, 15.5, 15.7],
        dii_pct=[16.2, 16.3, 16.1, 16.4, 16.5, 16.6, 16.7, 16.8],
        promoter_pct=[13.1] * 8,
        promoter_pledge=["No pledge"] * 8,
        sector_momentum=["neutral", "favor", "neutral", "neutral", "favor", "neutral", "favor", "neutral"],
    ),
    "NSE:HDFCBANK": dict(
        revenue_cr=[29500, 30100, 30800, 31300, 32000, 32700, 33300, 34100],
        operating_profit_cr=[15500, 15850, 16250, 16480, 16900, 17300, 17650, 18100],
        opm_pct=[3.9, 3.95, 4.0, 4.0, 4.05, 4.1, 4.1, 4.15],
        other_income_cr=[2100, 2150, 2200, 2180, 2250, 2300, 2320, 2380],
        net_profit_cr=[16200, 16500, 17100, 17300, 17800, 18400, 18700, 19300],
        eps=[21.3, 21.7, 22.5, 22.7, 23.4, 24.2, 24.6, 25.4],
        revenue_growth_pct=[10, 11, 12, 12, 13, 14, 14, 15],
        guidance_vs_delivered=["met", "met", "beat", "met", "met", "beat", "met", "beat"],
        growth_vs_sector_pp=[1, 1, 2, 2, 2, 3, 3, 3],
        margin_vs_sector_pp=[0.1, 0.1, 0.15, 0.15, 0.2, 0.2, 0.25, 0.25],
        fii_pct=[27.0, 27.2, 27.5, 27.3, 27.6, 27.8, 28.0, 28.2],
        dii_pct=[30.0, 30.1, 30.3, 30.2, 30.5, 30.6, 30.8, 31.0],
        promoter_pct=[0.0] * 8,
        promoter_pledge=["No promoter — professionally held"] * 8,
        sector_momentum=["neutral", "favor", "favor", "neutral", "favor", "favor", "neutral", "favor"],
    ),
    "NASDAQ:NVDA": dict(
        revenue_cr=[13500, 15400, 17800, 20200, 23400, 27100, 31200, 36000],
        operating_profit_cr=[7020, 8162, 9701, 11110, 13104, 15447, 18096, 21240],
        opm_pct=[52.0, 53.0, 54.5, 55.0, 56.0, 57.0, 58.0, 59.0],
        other_income_cr=[180, 190, 210, 220, 240, 260, 280, 300],
        net_profit_cr=[5600, 6500, 7700, 8900, 10500, 12400, 14600, 17200],
        eps=[2.26, 2.62, 3.10, 3.58, 4.23, 4.99, 5.88, 6.93],
        revenue_growth_pct=[45, 52, 58, 63, 70, 78, 85, 94],
        guidance_vs_delivered=["beat"] * 8,
        growth_vs_sector_pp=[20, 25, 28, 30, 35, 38, 42, 48],
        margin_vs_sector_pp=[15, 15.5, 16, 16.2, 16.8, 17.1, 17.5, 18.0],
        fii_pct=[68.0, 68.6, 69.1, 69.7, 70.2, 70.8, 71.4, 72.0],
        dii_pct=[None] * 8,
        promoter_pct=[4.0] * 8,
        promoter_pledge=["N/A — US insider holding, no pledge concept"] * 8,
        sector_momentum=["favor"] * 8,
    ),
    "NASDAQ:MSFT": dict(
        revenue_cr=[56500, 57800, 59100, 60600, 62200, 63500, 65100, 67000],
        operating_profit_cr=[24860, 25721, 26595, 27391, 28301, 29083, 29946, 30962],
        opm_pct=[44.0, 44.5, 45.0, 45.2, 45.5, 45.8, 46.0, 46.2],
        other_income_cr=[500, 520, 540, 550, 570, 580, 600, 620],
        net_profit_cr=[20500, 21100, 21800, 22400, 23100, 23800, 24600, 25500],
        eps=[2.76, 2.84, 2.93, 3.01, 3.11, 3.20, 3.31, 3.43],
        revenue_growth_pct=[12, 13, 13, 14, 15, 14, 15, 16],
        guidance_vs_delivered=["met", "beat", "met", "met", "beat", "met", "beat", "met"],
        growth_vs_sector_pp=[2, 2, 3, 3, 4, 3, 4, 5],
        margin_vs_sector_pp=[3, 3.2, 3.5, 3.6, 3.8, 4.0, 4.1, 4.3],
        fii_pct=[71.0, 71.2, 71.5, 71.8, 72.0, 72.3, 72.5, 72.8],
        dii_pct=[None] * 8,
        promoter_pct=[1.4] * 8,
        promoter_pledge=["N/A — US insider holding, no pledge concept"] * 8,
        sector_momentum=["favor", "favor", "neutral", "favor", "favor", "neutral", "favor", "favor"],
    ),
    "NASDAQ:AAPL": dict(
        revenue_cr=[89500, 90600, 91800, 92700, 93900, 95300, 96700, 98400],
        operating_profit_cr=[26850, 27362, 28009, 28376, 28920, 29543, 30170, 30902],
        opm_pct=[30.0, 30.2, 30.5, 30.6, 30.8, 31.0, 31.2, 31.4],
        other_income_cr=[300, 310, 320, 330, 340, 350, 360, 370],
        net_profit_cr=[22400, 22800, 23300, 23700, 24200, 24700, 25300, 25900],
        eps=[1.42, 1.45, 1.48, 1.51, 1.54, 1.57, 1.61, 1.65],
        revenue_growth_pct=[5, 6, 6, 7, 7, 8, 8, 9],
        guidance_vs_delivered=["met", "met", "met", "beat", "met", "met", "met", "met"],
        growth_vs_sector_pp=[0, 1, 1, 1, 2, 2, 2, 3],
        margin_vs_sector_pp=[1, 1, 1.2, 1.2, 1.3, 1.4, 1.4, 1.5],
        fii_pct=[60.0, 60.3, 60.5, 60.8, 61.0, 61.2, 61.4, 61.6],
        dii_pct=[None] * 8,
        promoter_pct=[0.06] * 8,
        promoter_pledge=["N/A — US insider holding, no pledge concept"] * 8,
        sector_momentum=["favor", "neutral", "favor", "favor", "neutral", "favor", "neutral", "favor"],
    ),
}

RATIOS = {
    "NSE:TATAMOTORS": dict(
        total_debt_cr=[40200, 39100, 37800, 36200], net_debt_cr=[31000, 29800, 28100, 26400], cash_cr=[9200, 9300, 9700, 9800],
        roe_pct=14.8, roce_pct=16.2, debt_equity=0.62, pe_current=24.3, pe_low_3yr=14.2, pe_high_3yr=31.5,
    ),
    "NSE:INFY": dict(
        total_debt_cr=[2100, 2000, 1900, 1800], net_debt_cr=[-8000, -8200, -8400, -8600], cash_cr=[10500, 10700, 10900, 11100],
        roe_pct=29.5, roce_pct=34.0, debt_equity=0.08, pe_current=26.8, pe_low_3yr=20.1, pe_high_3yr=29.4,
    ),
    "NSE:HDFCBANK": dict(
        total_debt_cr=[185000, 182000, 178000, 175000], net_debt_cr=[150000, 147000, 143000, 140000], cash_cr=[35000, 36000, 37000, 38000],
        roe_pct=17.2, roce_pct=17.2, debt_equity=6.8, pe_current=19.8, pe_low_3yr=15.2, pe_high_3yr=24.6,
    ),
    "NASDAQ:NVDA": dict(
        total_debt_cr=[9500, 9200, 8900, 8600], net_debt_cr=[-15000, -16200, -17800, -19500], cash_cr=[24500, 25400, 26700, 28100],
        roe_pct=78.5, roce_pct=82.0, debt_equity=0.15, pe_current=41.2, pe_low_3yr=18.4, pe_high_3yr=44.0,
    ),
    "NASDAQ:MSFT": dict(
        total_debt_cr=[48000, 47200, 46500, 45800], net_debt_cr=[5000, 3800, 2600, 1400], cash_cr=[43000, 43400, 43900, 44400],
        roe_pct=36.8, roce_pct=40.1, debt_equity=0.35, pe_current=32.6, pe_low_3yr=24.8, pe_high_3yr=36.2,
    ),
    "NASDAQ:AAPL": dict(
        total_debt_cr=[95000, 93500, 92000, 90500], net_debt_cr=[65000, 63200, 61400, 59600], cash_cr=[30000, 30300, 30600, 30900],
        roe_pct=145.2, roce_pct=52.3, debt_equity=1.45, pe_current=28.9, pe_low_3yr=22.1, pe_high_3yr=32.5,
    ),
}

# Market-wide price series (8 monthly points, all 6 stocks) — independent of any user's
# tracked positions. Previously this was wrongly derived from POSITIONS in seed.py; fixed
# here since price_history is a market.db (shared) table, not a per-user one.
PRICE_SERIES = {
    "NSE:TATAMOTORS": [780, 795, 810, 825, 840, 860, 875, 892],
    "NSE:INFY": [1780, 1795, 1805, 1815, 1825, 1835, 1842, 1850],
    "NSE:HDFCBANK": [1650, 1670, 1690, 1710, 1730, 1750, 1765, 1780],
    "NASDAQ:NVDA": [108, 113, 119, 124, 131, 127, 135, 142],
    "NASDAQ:MSFT": [395, 398, 402, 405, 408, 410, 412, 415],
    "NASDAQ:AAPL": [185, 187, 189, 190, 192, 193, 194, 195],
}
