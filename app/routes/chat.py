"""The app's first real JSON/streaming endpoint — deliberately breaking from the
htmx-partial-HTML convention every other route follows, for a real token-by-token chat
feel. See the plan doc for why.

Message history is round-tripped as an OPAQUE blob the client stores and replays
verbatim (pydantic_ai's own ModelMessagesTypeAdapter, not a hand-rolled {role,content}
shape) — avoids depending on reconstructing pydantic_ai's internal typed message format
from scratch, which is exactly the kind of thing that breaks silently across a library
version bump."""

import asyncio
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic_ai import Agent
from pydantic_ai.messages import BinaryContent, ModelMessagesTypeAdapter, PartDeltaEvent, PartStartEvent, TextPart, TextPartDelta

from app import positions as positions_mod
from app.ai.agent import agent, proposals_created, tools_used
from app.ai import errors
from app.ai.page_context import page_instructions, suggestions
from app import activity, statement_parse
from app.ai.providers import PROVIDERS, build_chain, get_user_settings

router = APIRouter()
logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_ATTACHMENTS = 5                    # files / photos in one message
MAX_TOTAL_BYTES = 40 * 1024 * 1024
MAX_TEXT_CHARS = 400_000
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
TEXT_TYPES = (".csv", ".tsv", ".txt")
SHEET_TYPES = (".csv", ".tsv", ".xlsx", ".xlsm", ".xls", ".pdf")


class FileRejected(Exception):
    pass


def _file_to_prompt(message: str, filename: str, data: bytes):
    """Builds the user prompt for an attached file. PDFs/images go to the model as native
    binary content (Gemini and Claude both read them directly — scanned PDFs included);
    CSV/text is inlined. Spreadsheets are rejected with a how-to rather than guessed at."""
    if len(data) > MAX_FILE_BYTES:
        raise FileRejected(f"That file is over {MAX_FILE_BYTES // (1024 * 1024)} MB — export a smaller date range.")
    name = filename.lower()
    note = f"[Attached file: {filename}] {message}"
    if name.endswith(".pdf"):
        return [note, BinaryContent(data=data, media_type="application/pdf")]
    for ext, media in IMAGE_TYPES.items():
        if name.endswith(ext):
            return [note, BinaryContent(data=data, media_type=media)]
    if name.endswith(TEXT_TYPES):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
        return f"{note}\n\n--- file contents ---\n{text[:MAX_TEXT_CHARS]}"
    if name.endswith((".xlsx", ".xlsm", ".xls")):
        try:
            text = statement_parse.sheets_to_text(statement_parse.read_sheets(filename, data), MAX_TEXT_CHARS)
        except Exception:  # noqa: BLE001 - corrupt / password-protected workbook
            raise FileRejected("I couldn't open that Excel file. Re-download it from your broker, or attach a CSV or PDF instead.")
        return f"{note}\n\n--- file contents ---\n{text}"
    if name.endswith((".heic", ".heif")):
        raise FileRejected("That iPhone photo format (HEIC) can't be read here. Use the camera button, which converts it, or take a screenshot instead.")
    raise FileRejected("Attach a PDF, CSV, or image (PNG/JPG) of your statement.")


def _strip_binary(messages):
    """Drops attached file bytes before the history goes back to the client. Without this
    the whole PDF (base64) would round-trip on every later turn and be re-billed by the
    model each time. The proposal it produced is persisted separately, so nothing is lost."""
    for msg in messages:
        if getattr(msg, "kind", None) != "request":
            continue
        for part in msg.parts:
            if part.part_kind == "user-prompt" and isinstance(part.content, list):
                part.content = [c for c in part.content if isinstance(c, str)]
    return messages


def _sse(event: str | None, data) -> str:
    payload = json.dumps(data) if not isinstance(data, str) else data
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {payload}\n\n"


async def _run_events(prompt, model, message_history, instructions):
    """Yields ("text", delta) as the answer streams, then ("done", all_messages). Walks the agent graph itself instead of using
    run_stream(): run_stream treats the FIRST text as the final answer, so a model that writes "Let me check..." and then calls a tool
    (free models do this constantly) had its answer cut off after the preamble. Here tools run, and the text after them streams too."""
    emitted = False
    async with agent.iter(prompt, model=model, message_history=message_history, instructions=instructions or None) as run:
        async for node in run:
            if not Agent.is_model_request_node(node):
                continue
            first = True
            async with node.stream(run.ctx) as events:
                async for ev in events:
                    text = None
                    if isinstance(ev, PartStartEvent) and isinstance(ev.part, TextPart):
                        text = ev.part.content
                    elif isinstance(ev, PartDeltaEvent) and isinstance(ev.delta, TextPartDelta):
                        text = ev.delta.content_delta
                    if not text:
                        continue
                    if first and emitted:
                        yield "text", "\n\n"  # a new model turn after tool calls: start a fresh paragraph
                    first = False
                    emitted = True
                    yield "text", text
        yield "done", run.result.all_messages()


def _files_to_prompt(message: str, uploads: list):
    """One prompt for several attachments: the user's words first, then each file in order (text and spreadsheets inlined, PDFs and images as binary)."""
    parts: list = [message]
    for i, (filename, data) in enumerate(uploads, 1):
        piece = _file_to_prompt(f"(attachment {i} of {len(uploads)})", filename, data)
        if isinstance(piece, list):
            parts.extend(piece)
        else:
            parts.append(piece)
    return parts


async def _stream(user_id: int, message: str, history_json: str | None, page: str | None, symbol: str | None, uploads=None):
    uploads = list(uploads or [])
    upload = uploads[0] if len(uploads) == 1 else None   # the single-file path below is unchanged
    if user_id is None:
        # AuthMiddleware deliberately lets an unauthenticated request through to this
        # path (an HTML redirect is useless mid-stream) rather than gating it — this is
        # where that case actually gets handled, as an SSE error frame.
        yield _sse("error", "Not logged in — please log in and try again.")
        return

    chain = build_chain(user_id)  # [primary, fallback?] — a link whose provider has no key is already dropped
    if not chain:
        s = get_user_settings(user_id)
        label = PROVIDERS.get(s["provider"], {}).get("label", s["provider"])
        yield _sse("error", f"No API key for {label}. Add one in AI settings (or pick a provider that has one).")
        return

    message_history = ModelMessagesTypeAdapter.validate_json(history_json) if history_json else None

    # The screen's data goes in as per-run instructions (rebuilt every turn, so it is never stale and never piles up in the history);
    # the short hint in the prompt itself keeps the model oriented when the user moves between pages mid-conversation.
    instructions = page_instructions(user_id, page, symbol)
    hint = f"[Now on the {page} page" + (f" for {symbol}" if symbol else "") + ".] " if page else ""

    prompt = hint + message
    activity.log(user_id, "step", "import_attached" if uploads else "chat_message",
                 detail=(",".join(sorted({u[0].rsplit(".", 1)[-1].lower()[:6] for u in uploads if "." in u[0]}))[:30] or None) if uploads else None)
    if len(uploads) > MAX_ATTACHMENTS or sum(len(u[1]) for u in uploads) > MAX_TOTAL_BYTES:
        yield _sse("error", f"Attach up to {MAX_ATTACHMENTS} files, {MAX_TOTAL_BYTES // (1024 * 1024)} MB in all.")
        return
    if uploads:
        try:  # the widget shows the processing notice before a file can be sent: keep the time of the first one
            from app.db import get_users_conn
            with get_users_conn() as u:
                u.execute("UPDATE users SET import_consent_at = COALESCE(import_consent_at, CURRENT_TIMESTAMP) WHERE id = ?", (user_id,))
                u.commit()
        except Exception:  # noqa: BLE001 - a logging failure must never block the import
            logger.warning("could not record import notice time")
    if len(uploads) > 1 and all(u[0].lower().endswith(SHEET_TYPES) for u in uploads):
        # Several statements: each is read column-by-column with no AI and the positions are proposed together (one review card).
        parsed_all = []
        try:
            for u in uploads:
                parsed_all.append(statement_parse.parse_statement(statement_parse.read_sheets(*u)))
        except statement_parse.NotPositions as e:
            yield _sse(None, {"type": "text", "delta": str(e)})
            yield "data: [DONE]\n\n"
            return
        except Exception:  # noqa: BLE001 - one unreadable file: the AI path below reads them all
            parsed_all = []
        if parsed_all and all(p is not None for p in parsed_all) and len({p.kind for p in parsed_all}) == 1:
            txns = [t for p in parsed_all for t in p.txns]
            markets = {p.market for p in parsed_all if p.market}
            market = next(iter(markets)) if len(markets) == 1 else None
            try:
                proposal = await asyncio.to_thread(positions_mod.propose, user_id, txns, None, market)
            except Exception as e:  # noqa: BLE001
                logger.warning("multi-file import failed: %s: %s", type(e).__name__, str(e)[:200])
                proposal = None
            if proposal is not None:
                intro = (f"Read your {len(parsed_all)} files directly ({len(txns)} rows in all). I found {len(proposal['rows'])} position"
                         f"{'s' if len(proposal['rows']) != 1 else ''}"
                         + (f" and {len(proposal['skipped'])} I couldn't place" if proposal["skipped"] else "") + ". "
                         "Check each line below, fix anything that's off, then confirm. Nothing is saved until you do.")
                if len(markets) > 1:
                    intro += " The files mix India and US holdings; check the market on each line."
                activity.log(user_id, "step", "import_proposed", detail=f"{len(proposal['rows'])} rows, {len(proposal['skipped'])} skipped")
                yield _sse(None, {"type": "text", "delta": intro})
                yield _sse(None, {"type": "proposal", "proposal": proposal})
                yield "data: [DONE]\n\n"
                return
    if upload is not None and upload[0].lower().endswith(SHEET_TYPES):
        # Spreadsheet exports map column-by-column with no AI: exact, free, and long files are no problem. Anything unrecognised
        # falls through to the AI reading the file's text.
        try:
            parsed = statement_parse.parse_statement(statement_parse.read_sheets(*upload))
        except statement_parse.NotPositions as e:
            yield _sse(None, {"type": "text", "delta": str(e)})
            yield "data: [DONE]\n\n"
            return
        except Exception:  # noqa: BLE001 - unreadable workbook: let the AI path report it
            parsed = None
        if parsed is not None:
            try:
                proposal = await asyncio.to_thread(positions_mod.propose, user_id, parsed.txns, None, parsed.market)
            except Exception as e:  # noqa: BLE001
                logger.warning("direct import failed: %s: %s", type(e).__name__, str(e)[:200])
                proposal = None
            if proposal is not None:
                n_rows = len(parsed.txns)
                intro = (f"Read your {parsed.kind} file directly ({n_rows} row{'s' if n_rows != 1 else ''}; {parsed.mapping}). "
                         f"I found {len(proposal['rows'])} position{'s' if len(proposal['rows']) != 1 else ''}"
                         + (f" and {len(proposal['skipped'])} I couldn't place" if proposal["skipped"] else "") + ". "
                         "Check each line below, fix anything that's off, then confirm. Nothing is saved until you do.")
                if parsed.skipped:
                    intro += f" ({parsed.skipped} non-equity or unrecognised rows were left out.)"
                for note in parsed.notes:
                    intro += " " + note
                activity.log(user_id, "step", "import_proposed", detail=f"{len(proposal['rows'])} rows, {len(proposal['skipped'])} skipped")
                yield _sse(None, {"type": "text", "delta": intro})
                yield _sse(None, {"type": "proposal", "proposal": proposal})
                yield "data: [DONE]\n\n"
                return
    if upload is not None:
        filename, data = upload
        try:
            prompt = _file_to_prompt(hint + (message or "Import my positions from this file."), filename, data)
        except FileRejected as e:
            yield _sse("error", str(e))
            return
        if isinstance(prompt, list):  # a PDF/image goes to the model as binary: only some providers can read it
            capable = [c for c in chain if c["files"]]
            if not capable:
                yield _sse("error", f"{chain[0]['label']} can't read PDFs or images. Attach a CSV instead, or switch to "
                                    "Gemini, Claude or OpenAI in AI settings (a fallback model that can read files also works).")
                return
            chain = capable

    if len(uploads) > 1:
        try:
            prompt = _files_to_prompt(hint + (message or "Import my positions from these files."), uploads)
        except FileRejected as e:
            yield _sse("error", str(e))
            return
        if any(not isinstance(x, str) for x in prompt):  # a PDF/image goes to the model as binary: only some providers can read it
            capable = [c for c in chain if c["files"]]
            if not capable:
                yield _sse("error", f"{chain[0]['label']} can't read PDFs or images. Attach CSV files instead, or switch to "
                                    "Gemini, Claude or OpenAI in AI settings (a fallback model that can read files also works).")
                return
            chain = capable

    created: list = []
    proposals_created.set(created)
    used: list = []
    tools_used.set(used)

    # Walk the chain: each model gets up to 3 tries for transient errors ("high demand" 503s are routine),
    # then the next model takes over (quota, credits, bad key, model gone...). Both only happen while NOTHING
    # has been sent to the client yet, so switching can never duplicate or splice text.
    last_error, last_label = None, None
    for link in chain:
        for attempt in range(3):
            sent_any = False
            announced = False
            reported = 0  # how many tool calls the client already knows about
            try:
                history = None
                async for kind, val in _run_events(prompt, link["model"], message_history, instructions):
                    if kind == "text":
                        if not announced:
                            announced = True
                            yield _sse(None, {"type": "model", "label": link["label"], "fallback": link["role"] == "fallback"})
                        if len(used) > reported:  # tools run before the first token - surface them as the answer starts
                            yield _sse(None, {"type": "tools", "items": used[reported:]})
                            reported = len(used)
                        sent_any = True
                        yield _sse(None, {"type": "text", "delta": val})
                    else:
                        history = ModelMessagesTypeAdapter.dump_json(_strip_binary(val)).decode()
                if not announced:
                    yield _sse(None, {"type": "model", "label": link["label"], "fallback": link["role"] == "fallback"})
                if len(used) > reported:
                    yield _sse(None, {"type": "tools", "items": used[reported:]})
                if not sent_any:
                    raise RuntimeError("the model returned no answer")
                yield _sse(None, {"type": "history", "messages": history})
                for proposal_id in created:
                    proposal = positions_mod.get_proposal(user_id, proposal_id)
                    if proposal:
                        yield _sse(None, {"type": "proposal", "proposal": proposal})
                yield "data: [DONE]\n\n"
                return
            except Exception as e:  # noqa: BLE001 — a model/tool-call failure must reach the widget as a visible error, not a dropped connection
                logger.warning("chat stream: %s failed (attempt %d): %s: %s", link["label"], attempt + 1, type(e).__name__, str(e)[:300])
                used.clear()
                if sent_any:  # the answer already started streaming — can't switch models mid-sentence
                    yield _sse("error", errors.friendly_error(e, link["label"]))
                    return
                last_error, last_label = e, link["label"]
                if attempt < 2 and errors.is_transient(e):
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                break  # not transient (or retries used up) → next model in the chain

    yield _sse("error", errors.friendly_error(last_error, last_label) if last_error else "Something went wrong. Try again.")


@router.post("/chat/stream")
async def chat_stream(request: Request):
    uploads: list = []
    if request.headers.get("content-type", "").startswith("application/json"):
        body = await request.json()
        message = body.get("message", "")
        history_json = body.get("history")
        page = body.get("page")
        symbol = body.get("symbol")
    else:
        # multipart (the widget — always, since it may carry a file) or plain form-encoded
        form = await request.form()
        message = form.get("message", "") or ""
        history_json = form.get("history") or None
        page = form.get("page") or None
        symbol = form.get("symbol") or None
        files = [f for f in form.getlist("files") if getattr(f, "filename", "")]
        legacy = form.get("file")
        if legacy is not None and getattr(legacy, "filename", ""):
            files.append(legacy)
        for f in files[:MAX_ATTACHMENTS + 1]:   # one past the limit, so _stream can say it is too many
            uploads.append((f.filename, await f.read()))

    return StreamingResponse(
        _stream(request.state.user_id, message, history_json, page, symbol, uploads),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/chat/suggestions")
def chat_suggestions(request: Request, page: str | None = None, symbol: str | None = None):
    """Starter questions for the screen the user is on, built from that screen's own data."""
    return suggestions(request.state.user_id, page, symbol)


@router.get("/chat/stock-search")
def stock_search(request: Request, q: str = ""):
    """Stock picker for the review card (fix a wrong match, place a row we could not match)."""
    if request.state.user_id is None:
        return JSONResponse({"error": "Not logged in."}, status_code=401)
    q = q.strip()
    if len(q) < 2:
        return []
    from app.db import get_market_conn
    like = f"%{q}%"
    with get_market_conn() as m:
        rows = m.execute("SELECT symbol, name FROM stocks WHERE symbol LIKE ? OR name LIKE ? ORDER BY (symbol LIKE 'NSE:%') DESC, (symbol LIKE ?) DESC, symbol LIMIT 8",
                         (like, like, f"{q.upper()}%")).fetchall()
    return [{"symbol": r["symbol"], "name": r["name"]} for r in rows]


@router.post("/chat/proposals/{proposal_id}/confirm")
async def confirm_proposal(request: Request, proposal_id: int):
    """The ONLY path that turns a proposal into tracked positions — a user click, never an LLM tool call, so the model can't write to a
    portfolio on its own. The body (optional) carries the user's corrections from the review card; they are re-validated server-side."""
    if request.state.user_id is None:
        return JSONResponse({"error": "Not logged in."}, status_code=401)
    try:
        edits = await request.json()
    except Exception:  # noqa: BLE001 - no body = confirm as proposed
        edits = None
    try:
        result = positions_mod.commit(request.state.user_id, proposal_id, edits if isinstance(edits, dict) else None)
    except positions_mod.EditError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    if result is None:
        return JSONResponse({"error": "That import is no longer pending (already confirmed, cancelled, or replaced)."}, status_code=409)
    return result


@router.post("/chat/proposals/{proposal_id}/cancel")
def cancel_proposal(request: Request, proposal_id: int):
    if request.state.user_id is None:
        return JSONResponse({"error": "Not logged in."}, status_code=401)
    positions_mod.cancel(request.state.user_id, proposal_id)
    return {"cancelled": True}
