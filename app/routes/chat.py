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
from pydantic_ai.messages import BinaryContent, ModelMessagesTypeAdapter

from app import positions as positions_mod
from app.ai.agent import agent, proposals_created, tools_used
from app.ai import errors
from app.ai.providers import PROVIDERS, build_chain, get_user_settings

router = APIRouter()
logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TEXT_CHARS = 400_000
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
TEXT_TYPES = (".csv", ".tsv", ".txt")


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
    if name.endswith((".xlsx", ".xls")):
        raise FileRejected("Excel files aren't supported yet — save it as CSV or PDF and attach that instead.")
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


async def _stream(user_id: int, message: str, history_json: str | None, page: str | None, symbol: str | None, upload=None):
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

    # page/symbol are only a hint prepended to the prompt — the agent still calls its
    # own tools for real data, never trusting this as the actual financial fact.
    hint = ""
    if page == "analytics" and symbol:
        hint = f"[The user is currently viewing the Analytics page for {symbol}.] "
    elif page in ("management", "portfolio"):
        hint = f"[The user is currently viewing the {page.capitalize()} page (their tracked positions/watchlist).] "

    prompt = hint + message
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
                async with agent.run_stream(prompt, model=link["model"], message_history=message_history) as result:
                    async for text in result.stream_text(delta=True):
                        if not announced:
                            announced = True
                            yield _sse(None, {"type": "model", "label": link["label"], "fallback": link["role"] == "fallback"})
                        if len(used) > reported:  # tools run before the first token — surface them as the answer starts
                            yield _sse(None, {"type": "tools", "items": used[reported:]})
                            reported = len(used)
                        sent_any = True
                        yield _sse(None, {"type": "text", "delta": text})
                    if not announced:
                        yield _sse(None, {"type": "model", "label": link["label"], "fallback": link["role"] == "fallback"})
                    if len(used) > reported:
                        yield _sse(None, {"type": "tools", "items": used[reported:]})
                    history = ModelMessagesTypeAdapter.dump_json(_strip_binary(result.all_messages())).decode()
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
    upload = None
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
        file = form.get("file")
        if file is not None and getattr(file, "filename", ""):
            upload = (file.filename, await file.read())

    return StreamingResponse(
        _stream(request.state.user_id, message, history_json, page, symbol, upload),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/chat/proposals/{proposal_id}/confirm")
def confirm_proposal(request: Request, proposal_id: int):
    """The ONLY path that turns a proposal into tracked positions — a user click, never
    an LLM tool call, so the model can't write to a portfolio on its own."""
    if request.state.user_id is None:
        return JSONResponse({"error": "Not logged in."}, status_code=401)
    result = positions_mod.commit(request.state.user_id, proposal_id)
    if result is None:
        return JSONResponse({"error": "That import is no longer pending (already confirmed, cancelled, or replaced)."}, status_code=409)
    return result


@router.post("/chat/proposals/{proposal_id}/cancel")
def cancel_proposal(request: Request, proposal_id: int):
    if request.state.user_id is None:
        return JSONResponse({"error": "Not logged in."}, status_code=401)
    positions_mod.cancel(request.state.user_id, proposal_id)
    return {"cancelled": True}
