from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.db import get_market_conn
from app.templates_config import templates

router = APIRouter()


@router.get("/search-stocks", response_class=HTMLResponse)
def search_stocks(request: Request, q: Optional[str] = None):
    """Market-wide symbol/name search (not capped to admin's top-100 default) — powers
    the topbar search used from Analytics and every other screen. Only stocks with at
    least one quarterly row are listed: ~5,800 universe entries (funds, SPACs, ADRs,
    unreported small caps) have none, and opening one just renders an empty page. They stay
    in `stocks` (prices, tracking and later ingestion still use them) — only search hides them."""
    results = []
    if q and len(q.strip()) >= 1:
        like = f"%{q.strip()}%"
        with get_market_conn() as m:
            results = m.execute(
                """
                SELECT symbol, name, market,
                       (SELECT COUNT(*) FROM quarterly_data qd WHERE qd.symbol = stocks.symbol) AS quarters
                FROM stocks
                WHERE (symbol LIKE ? OR name LIKE ?)
                  AND EXISTS (SELECT 1 FROM quarterly_data qd WHERE qd.symbol = stocks.symbol)
                ORDER BY (symbol LIKE ?) DESC, symbol
                LIMIT 12
                """,
                (like, like, f"%{q.strip().upper()}%"),
            ).fetchall()
    return templates.TemplateResponse("_stock_search_results.html", {"request": request, "results": results, "q": q})
