"""Checks on model-written text, shared by the daily story and the deep-research report: strict figure matching against the source text,
banned phrases, and dash clean-up. Nothing here changes a figure; it removes dashes and reports what it found."""
import re

BANNED = ("it's important to note", "in today's", "in the current environment", "deep dive", "delve", "landscape", "robust", "compelling", "navigate",
          "poised to", "well-positioned", "exciting", "remarkable journey", "multifaceted", "tapestry", "in conclusion", "overall", "it remains to be seen",
          "game-changer", "unlock value", "easy to miss", "the real story", "you are betting", "here's the catch", "let that sink in")

DRIVER_TABLE = """Value drivers by business model (choose the row that fits the company; track only the drivers we hold figures for, report the rest as unknown):
- Banks: net interest margin, CASA ratio, credit growth, gross and net NPA, provision cover, ROE
- NBFC / housing finance: AUM, disbursement growth, gross NPA, cost of funds, spread, ROA
- Jewellery / B2B manufacturing: volume, realisation, mix, working-capital days
- Industrials / commodities: volume, realisation, cost per unit, capacity utilisation, commodity spread
- Retail: same-store sales, store count, gross margin, post-rent EBITDA
- IT services: constant-currency growth, EBIT margin, attrition, deal wins
- Asset-light fees / AMC / platforms: AUM, net flows, fee yield, recurring share, cost-to-income
- Consumer goods and pharma: volume growth, price/mix, gross margin, advertising or R&D share, receivable and inventory days"""

_NUM = re.compile(r"(?<![\w.])\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![\w.])\d+(?:\.\d+)?")


def numbers(text: str) -> list[tuple[float, int]]:
    """Every figure in the text as (value, decimals shown). Citations, FY/Q labels and source ids are not figures."""
    text = re.sub(r"\[S[\d, S]+\]", " ", text)
    text = re.sub(r"\b(?:FY|Q)\s?\d{1,4}\b|\bS\d+\b", " ", text)
    out = []
    for m in _NUM.finditer(text):
        tok = m.group().replace(",", "")
        try:
            out.append((float(tok), len(tok.split(".")[1]) if "." in tok else 0))
        except ValueError:
            pass
    return out


def clean_dashes(s: str) -> tuple[str, int]:
    """Em and en dashes out (ranges become 20-31, the rest become commas); stray tags like [EVIDENCE LIMITS] out. Returns (text, dashes removed)."""
    s = re.sub(r"\s?\[(?!S\d)[A-Za-z ,]+\]", "", s)
    n = len(re.findall(r"[—–]", s))
    s = re.sub(r"(?<=\d)\s?[–—]\s?(?=\d)", "-", s)
    s = re.sub(r"\s?[—–]\s?", ", ", s)
    return s, n


def figures_not_in_sources(text: str, source_text: str) -> tuple[list[float], int]:
    """(figures that are not a source figure rounded to the digits shown, how many figures were counted). Years and small counts are skipped."""
    have = [v for v, _ in numbers(source_text)]
    unmatched, counted = [], 0
    for x, dec in numbers(text):
        if (1900 <= x <= 2100 and x == int(x)) or (dec == 0 and x <= 12):
            continue
        counted += 1
        half = 0.5 * 10 ** (-dec) + 1e-9
        if not any(abs(x - y) <= half for y in have):
            unmatched.append(x)
    return unmatched, counted


def banned_in(text: str) -> list[str]:
    low = text.lower()
    return [b for b in BANNED if b in low]


def clean_strings(obj, counter: list):
    """Walk a dict/list and clean every string in place of a copy; counter[0] accumulates dashes removed."""
    if isinstance(obj, str):
        s, n = clean_dashes(obj)
        counter[0] += n
        return s.strip()
    if isinstance(obj, list):
        return [clean_strings(x, counter) for x in obj]
    if isinstance(obj, dict):
        return {k: clean_strings(v, counter) for k, v in obj.items()}
    return obj


def strings_of(obj) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, list):
        return [s for x in obj for s in strings_of(x)]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in strings_of(v)]
    return []
