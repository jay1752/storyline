"""Reads the table out of a text-based broker PDF so the same column mapper that handles Excel/CSV can read it (app/statement_parse.py).

Most broker PDFs (5paisa, INDstocks/INDmoney, Zerodha holdings, depository statements) carry a real text layer. pdfplumber gives every word's
position, so a row's cells are assigned to columns by where they sit (the data's own column geometry), not by counting gaps - an empty cell
would otherwise shift everything after it left. Scanned PDFs have no text layer: this returns [] and the chat
falls back to the AI reading the page image.

Output has the shape read_sheets() uses: [(name, rows)], rows = list of cell lists, header row first."""
import io
import re

from app.statement_parse import _map_columns

_NUMBER = re.compile(r"^[-(]?[\d,]+(\.\d+)?\)?$")
CELL_GAP = 3.6   # points: words closer than this are one cell, a wider gap starts the next cell


def _line_cells(words):
    """[(x0, x1, text)] cells of one text line from its words (sorted left to right)."""
    out = []
    for w in sorted(words, key=lambda w: w["x0"]):
        word = (w["x0"], w["x1"], w["text"])
        if out and w["x0"] - out[-1][1] <= CELL_GAP:
            out[-1] = (out[-1][0], w["x1"], out[-1][2] + " " + w["text"], out[-1][3] + [word])
        else:
            out.append((w["x0"], w["x1"], w["text"], [word]))
    return out


def _page_lines(page):
    words = page.extract_words(x_tolerance=1.5, y_tolerance=2, keep_blank_chars=False, use_text_flow=False)
    lines: list[list] = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        if lines and abs(lines[-1][0]["top"] - w["top"]) <= 2.5:
            lines[-1].append(w)
        else:
            lines.append([w])
    return [_line_cells(l) for l in lines]


def _center(t):
    return (t[0] + t[1]) / 2


def _data_columns(data_lines):
    """Column intervals found in the data itself: tokens that overlap (or touch) across rows belong to one column. Headers are often
    centred over right-aligned numbers, so the data's own geometry is the reliable thing to align on."""
    # only lines that look like table rows shape the columns: 3+ cells and a bare number among them (paragraph text and page furniture
    # have neither, and one justified legal-text line would otherwise fuse every column together)
    rowlike = [toks for toks in data_lines if len(toks) >= 3 and any(_NUMBER.match(t[2]) for t in toks)]
    spans = sorted((t[0], t[1]) for toks in rowlike for t in toks)
    cols: list[list[int]] = []
    for s0, e0 in spans:
        if cols and s0 <= cols[-1][1] + 0.5:
            cols[-1][1] = max(cols[-1][1], e0)
        else:
            cols.append([s0, e0])
    return cols


def _split_header(header, dcols):
    """A header cell whose words sit over two different data columns is really two headers that were printed close together
    ("Price" and "Tax/Charges"): split it. Words that sit over no data column (a centred header over right-aligned numbers) stay put."""
    def col_of(w):
        return next((k for k, d in enumerate(dcols) if w[0] <= d[1] and w[1] >= d[0]), None)
    out = []
    for h in header:
        groups: list[list] = []
        last = None
        for w in h[3]:
            c = col_of(w)
            if groups and (c is None or c == last or last is None):
                groups[-1].append(w)
                last = c if c is not None else last
            else:
                groups.append([w])
                last = c
        if len(groups) > 1:
            out.extend((g[0][0], g[-1][1], " ".join(x[2] for x in g), g) for g in groups)
        else:
            out.append(h)
    return out


def _column_map(header, dcols):
    """dcols index -> header index. Same count: by order. Otherwise (empty columns in the data): nearest edge/centre, one data column
    per header."""
    if len(dcols) == len(header):
        return list(range(len(header)))
    mapping, used = [], set()
    for d in dcols:
        order = sorted(range(len(header)), key=lambda hi: min(abs(d[0] - header[hi][0]), abs(d[1] - header[hi][1]), abs((d[0] + d[1]) / 2 - _center(header[hi]))))
        pick = next((hi for hi in order if hi not in used), order[0])
        used.add(pick)
        mapping.append(pick)
    return mapping


def pdf_sheets(data: bytes, max_pages: int = 60) -> list[tuple[str, list[list]]]:
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = [_page_lines(p) for p in pdf.pages[:max_pages]]
    except Exception:  # noqa: BLE001 - encrypted / unreadable PDF: the AI path reports it
        return []
    header = None
    data_lines: list = []   # token lists of every non-header line after the header
    for lines in pages:
        i = 0
        while i < len(lines):
            toks = lines[i]
            i += 1
            if not toks:
                continue
            if _map_columns([t[2] for t in toks]) is not None and len(toks) >= 4:
                # a header line (first page, or repeated on each page). Wrapped header words sit on the next line under their column.
                hdr = list(toks)
                while i < len(lines):
                    nxt = lines[i]
                    if not nxt or len(nxt) > 3 or any(len(t[2]) > 12 for t in nxt) or any(c.isdigit() for t in nxt for c in t[2]):
                        break
                    for t in nxt:
                        k = min(range(len(hdr)), key=lambda ci: abs(_center(hdr[ci]) - _center(t)))
                        hdr[k] = (min(hdr[k][0], t[0]), max(hdr[k][1], t[1]), hdr[k][2] + " " + t[2], hdr[k][3] + t[3])
                    i += 1
                if header is None:
                    header = hdr
                continue
            if header is not None:
                data_lines.append(toks)
    if header is None or not data_lines:
        return []
    dcols = _data_columns(data_lines)
    header = _split_header(header, dcols)
    cmap = _column_map(header, dcols)
    rows: list[list] = [[h[2] for h in header]]
    for toks in data_lines:
        cells = [None] * len(header)
        for t in toks:
            di = next((k for k, d in enumerate(dcols) if t[0] <= d[1] and t[1] >= d[0]), None)
            if di is None:
                continue
            hi = cmap[di]
            cells[hi] = (cells[hi] + " " + t[2]) if cells[hi] else t[2]
        rows.append(cells)
    return [("pdf", rows)] if len(rows) > 1 else []
