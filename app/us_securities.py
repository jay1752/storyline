"""Tell a company's own stock from the other lines listed under its name: warrants, units, rights and preferred shares. Those carry no
filings of their own and copy the parent's fundamentals, so they are left out of the discovery screens, the SEC annual fill and the universe."""
import re

_DASH = re.compile(r"P[A-Z]?|UN?|WT?S?[A-Z]?|WW|RT?|R")  # X-PA preferred, X-UN unit, X-WT warrant, X-RT right ...
_SUFFIXES = ("WW", "WS", "W", "RT", "R", "U")


def _norm(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def is_derivative(ticker: str, names: dict[str, str]) -> bool:
    """`ticker` without the exchange prefix; `names` = {US ticker: company name} for every US stock we hold.
    A dash suffix of the SEC's preferred/unit/warrant/right form (X-PA, X-UN, X-WT) is a derivative line (BRK-B is a share class and is not).
    A trailing W/WS/R/U counts ONLY when the base ticker exists AND carries the very same company name (ALURW is Allurion's warrant;
    DHR, PGR, MAR, INTU, ITW are real companies that merely end in R/W/U)."""
    if "-" in ticker:
        return bool(_DASH.fullmatch(ticker.rsplit("-", 1)[1]))
    me = _norm(names.get(ticker))
    if not me:
        return False
    for suf in _SUFFIXES:
        if ticker.endswith(suf) and len(ticker) > len(suf) + 1:
            base = ticker[:-len(suf)]
            if _norm(names.get(base)) == me:
                return True
    return False
