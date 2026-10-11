"""Tell a company's own stock from the other lines listed under its name: warrants, units, rights and preferred shares. Those carry no
filings of their own and copy the parent's fundamentals, so they are left out of the discovery screens and of the SEC annual fill."""
import re

_DASH = re.compile(r"P[A-Z]?|UN?|WT?S?[A-Z]?|WW|RT?|R")  # X-PA preferred, X-UN unit, X-WT warrant, X-RT right ...
_SUFFIXES = (("WW", "warrant"), ("WS", "warrant"), ("W", "warrant"), ("RT", "right"), ("R", "right"), ("U", "unit"))


def is_derivative(ticker: str, tickers: set[str]) -> bool:
    """`ticker` without the exchange prefix; `tickers` = every US ticker we hold. A trailing W/WS/R/U only counts when the base ticker exists
    too (PANW, SCHW, SNOW are real companies), a dash suffix only when it has the SEC's preferred/unit/warrant/right form (BRK-B is a share class)."""
    if "-" in ticker:
        return bool(_DASH.fullmatch(ticker.rsplit("-", 1)[1]))
    for suf, _ in _SUFFIXES:
        if ticker.endswith(suf) and len(ticker) > len(suf) + 1 and (ticker[:-len(suf)] in tickers or ticker[:-len(suf)] + "U" in tickers):
            return True
    return False
