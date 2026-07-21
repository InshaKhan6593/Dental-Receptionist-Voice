"""FastAPI app. Wires: mock PMS API, the ADK voice agent on Gemini Live,
Postgres-backed sessions (message persistence), a Twilio Media Streams bridge,
a browser bridge, and LangSmith per-call voice tracing.

Endpoints:
  POST /twilio/voice     -> TwiML that connects the call to /twilio/stream
  WS   /twilio/stream    -> Twilio Media Streams (mu-law 8k) <-> Gemini Live
  WS   /ws/{user_id}     -> browser mic test (PCM16 16k <-> 24k)
  POST /clinic           -> set the clinic name (customise the demo, no UI needed)
  GET  /                 -> browser voice test client (static/)
  GET  /healthz          -> health
  ...  /pms/v1/*         -> mock Dentally API
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
from datetime import datetime
from pathlib import Path

from fastapi import (FastAPI, HTTPException, Request, Response, WebSocket,
                     WebSocketDisconnect)
from fastapi.staticfiles import StaticFiles
from google.adk.agents.live_request_queue import LiveRequestQueue
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types

from . import runtime
from .agent import build_agent
from .cache import ReferenceCache
from .config import APP_NAME, PMS_BASE_URL, SESSION_DB_URL, load_clinic
from .models import CallLog, SessionLocal, init_db
from .pms import PMSClient
from .pms import router as pms_router
from .telephony import pcm24k_to_ulaw8k, ulaw8k_to_pcm16k
from .tracing import CallRecorder, log_call, start_call

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("dental.main")

app = FastAPI(title=APP_NAME)
app.include_router(pms_router)

_runner: Runner | None = None


def _build_runner() -> Runner:
    """(Re)build the runner from the current clinic config."""
    global _runner
    _runner = Runner(
        app_name=APP_NAME,
        agent=build_agent(runtime.CLINIC),
        session_service=DatabaseSessionService(db_url=SESSION_DB_URL),
    )
    return _runner


@app.on_event("startup")
async def _startup():
    init_db()
    runtime.CLINIC = load_clinic()
    runtime.PMS = PMSClient(PMS_BASE_URL)
    runtime.CACHE = ReferenceCache(clinic=runtime.CLINIC)
    runtime.CACHE.load_from_db()          # bootstrap reference cache (no self-HTTP)
    _build_runner()
    logger.info("Ready - clinic=%s, model=%s", runtime.CLINIC["name"],
                runtime.CLINIC.get("pms", {}).get("type"))


@app.on_event("shutdown")
async def _shutdown():
    if runtime.PMS:
        await runtime.PMS.aclose()


# ------------------------------ live session --------------------------------
async def _start_session(user_id: str, state: dict):
    # Open the call's parent LangSmith run first, so the PMS tool runs that follow
    # nest under it (a waterfall). No-op unless tracing is enabled.
    start_call(state.get("call_sid"), state.get("clinic_id"), state.get("caller_number"))
    session = await _runner.session_service.create_session(
        app_name=APP_NAME, user_id=user_id, state=state)
    run_config = RunConfig(
        streaming_mode=StreamingMode.BIDI,
        response_modalities=["AUDIO"],
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        # survive the ~10-min socket reset and the ~15-min context limit
        # (handle-based resumption; transparent=True is Vertex AI-only)
        session_resumption=types.SessionResumptionConfig(),
        context_window_compression=types.ContextWindowCompressionConfig(
            sliding_window=types.SlidingWindow()),
    )
    queue = LiveRequestQueue()
    events = _runner.run_live(user_id=user_id, session_id=session.id,
                              live_request_queue=queue, run_config=run_config)
    # Gemini Live is reactive - it stays silent until it receives a turn. Send a
    # synthetic turn (never heard by the caller) so the agent greets first.
    queue.send_content(types.Content(
        role="user",
        parts=[types.Part(text="(The call has just connected. Greet the caller.)")]))
    return events, queue, session


async def _finalize(user_id: str, session_id: str, call_sid: str, caller_number: str,
                    recorder: CallRecorder, transcript: list[tuple[str, str]]):
    """Write the call record + push the voice-recording trace to LangSmith."""
    try:
        session = await _runner.session_service.get_session(
            app_name=APP_NAME, user_id=user_id, session_id=session_id)
        state = session.state if session else {}
    except Exception:
        state = {}
    text = "\n".join(f"{who}: {t}" for who, t in transcript)
    disposition = state.get("disposition") or "abandoned"
    with SessionLocal() as db:
        db.add(CallLog(
            call_sid=call_sid, clinic_id=runtime.CLINIC["clinic_id"],
            caller_number=caller_number, intent=state.get("intent"),
            caller_type=state.get("caller_type"), verified=bool(state.get("verified")),
            patient_id=state.get("patient_id"), disposition=disposition,
            escalated=bool(state.get("escalated")), transcript=text,
            ended_at=datetime.utcnow()))
        db.commit()
    log_call(clinic_id=runtime.CLINIC["clinic_id"], call_sid=call_sid,
             caller_number=caller_number, disposition=disposition,
             verified=bool(state.get("verified")), transcript=text, recorder=recorder)
    logger.info("Call %s ended - disposition=%s", call_sid, disposition)


def _collect_transcripts(event, transcript: list):
    it = getattr(event, "input_transcription", None)
    if it and getattr(it, "text", None):
        transcript.append(("caller", it.text))
    ot = getattr(event, "output_transcription", None)
    if ot and getattr(ot, "text", None):
        transcript.append(("agent", ot.text))


# ------------------------------ Twilio bridge -------------------------------
def _twilio_signature_ok(request: Request, form: dict) -> bool:
    """Verify Twilio signed this webhook (HMAC-SHA1 over the full URL + sorted
    POST params, per Twilio's spec). Without this, anyone who finds the public
    URL can open live Gemini sessions on our quota.

    Skipped only when TWILIO_AUTH_TOKEN is unset (local dev / browser demo).
    Heroku terminates TLS, so rebuild the public https URL from X-Forwarded-*
    rather than trusting the raw request scheme.
    """
    token = os.getenv("TWILIO_AUTH_TOKEN")
    if not token:
        logger.warning("TWILIO_AUTH_TOKEN unset - webhook signature NOT verified")
        return True

    signature = request.headers.get("X-Twilio-Signature", "")
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host", "")
    url = os.getenv("PUBLIC_BASE_URL") or f"{proto}://{host}"
    url = url.rstrip("/") + request.url.path

    payload = url + "".join(f"{k}{form[k]}" for k in sorted(form))
    digest = hmac.new(token.encode(), payload.encode("utf-8"), hashlib.sha1).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature)


@app.post("/twilio/voice")
async def twilio_voice(request: Request):
    """TwiML: bridge the inbound call to our Media Streams WebSocket, forwarding
    the caller's number so the session can seed caller ID."""
    form = dict(await request.form())
    if not _twilio_signature_ok(request, form):
        logger.warning("Rejected /twilio/voice - bad or missing Twilio signature")
        raise HTTPException(status_code=403, detail="invalid Twilio signature")
    caller = form.get("From", "unknown")
    host = request.headers.get("host")
    ws_url = f"wss://{host}/twilio/stream"
    twiml = ('<?xml version="1.0" encoding="UTF-8"?><Response><Connect>'
             f'<Stream url="{ws_url}"><Parameter name="from" value="{caller}"/></Stream>'
             '</Connect></Response>')
    return Response(content=twiml, media_type="text/xml")


@app.websocket("/twilio/stream")
async def twilio_stream(ws: WebSocket):
    await ws.accept()
    recorder = CallRecorder()
    transcript: list = []
    stream_sid = {"v": None}
    out_state = {"v": None}
    in_state = {"v": None}
    events = queue = session = None
    call_sid = caller_number = None
    agent_task = None

    async def pump_agent():
        async for event in events:
            if getattr(event, "interrupted", False):
                if stream_sid["v"]:
                    await ws.send_text(json.dumps({"event": "clear", "streamSid": stream_sid["v"]}))
                continue
            _collect_transcripts(event, transcript)
            content = getattr(event, "content", None)
            if not content or not content.parts:
                continue
            for part in content.parts:
                inline = getattr(part, "inline_data", None)
                if inline and inline.data and inline.mime_type.startswith("audio/"):
                    recorder.add_agent(inline.data)
                    mulaw, out_state["v"] = pcm24k_to_ulaw8k(inline.data, out_state["v"])
                    await ws.send_text(json.dumps({
                        "event": "media", "streamSid": stream_sid["v"],
                        "media": {"payload": base64.b64encode(mulaw).decode()}}))

    try:
        while True:
            msg = json.loads(await ws.receive_text())
            ev = msg.get("event")
            if ev == "start":
                start = msg["start"]
                stream_sid["v"] = start.get("streamSid") or msg.get("streamSid")
                call_sid = start.get("callSid", stream_sid["v"])
                caller_number = (start.get("customParameters") or {}).get("from") or "unknown"
                events, queue, session = await _start_session(
                    user_id=caller_number,
                    state={"call_sid": call_sid, "caller_number": caller_number,
                           "clinic_id": runtime.CLINIC["clinic_id"], "verified": False})
                agent_task = asyncio.create_task(pump_agent())
            elif ev == "media" and queue is not None:
                mulaw = base64.b64decode(msg["media"]["payload"])
                pcm16, in_state["v"] = ulaw8k_to_pcm16k(mulaw, in_state["v"])
                recorder.add_caller(pcm16)
                queue.send_realtime(types.Blob(mime_type="audio/pcm;rate=16000", data=pcm16))
            elif ev == "stop":
                break
    except WebSocketDisconnect:
        pass
    finally:
        if agent_task:
            agent_task.cancel()
        if queue:
            queue.close()
        if session:
            await _finalize(caller_number, session.id, call_sid or "twilio",
                            caller_number or "unknown", recorder, transcript)


# ------------------------------ browser bridge ------------------------------
@app.websocket("/ws/{user_id}")
async def browser_ws(ws: WebSocket, user_id: str):
    """Browser mic test: client sends base64 PCM16 16k, receives base64 PCM16 24k."""
    await ws.accept()
    recorder = CallRecorder()
    transcript: list = []
    call_sid = f"web-{user_id}"
    events, queue, session = await _start_session(
        user_id=user_id,
        state={"call_sid": call_sid, "caller_number": user_id,
               "clinic_id": runtime.CLINIC["clinic_id"], "verified": False})

    async def agent_to_client():
        async for event in events:
            if getattr(event, "interrupted", False):
                await ws.send_text(json.dumps({"type": "interrupted"}))
                continue
            _collect_transcripts(event, transcript)
            content = getattr(event, "content", None)
            if not content or not content.parts:
                continue
            for part in content.parts:
                inline = getattr(part, "inline_data", None)
                if inline and inline.data and inline.mime_type.startswith("audio/"):
                    recorder.add_agent(inline.data)
                    await ws.send_text(json.dumps(
                        {"type": "audio", "data": base64.b64encode(inline.data).decode()}))

    async def client_to_agent():
        while True:
            message = json.loads(await ws.receive_text())
            if message.get("type") == "audio":
                pcm = base64.b64decode(message["data"])
                recorder.add_caller(pcm)
                queue.send_realtime(types.Blob(mime_type="audio/pcm;rate=16000", data=pcm))
            elif message.get("type") == "text":
                queue.send_content(types.Content(
                    role="user", parts=[types.Part(text=message["data"])]))

    try:
        await asyncio.gather(agent_to_client(), client_to_agent())
    except WebSocketDisconnect:
        logger.info("Browser client %s disconnected", user_id)
    finally:
        queue.close()
        await _finalize(user_id, session.id, call_sid, user_id, recorder, transcript)


# ------------------------------ admin / health ------------------------------
@app.post("/clinic")
async def set_clinic(payload: dict):
    """Customise the demo without a UI: POST {"name": "Bright Smile Dental"}.
    Updates the clinic name and rebuilds the agent's prompt."""
    name = (payload or {}).get("name")
    if name:
        runtime.CLINIC["name"] = name
        _build_runner()
    return {"clinic": runtime.CLINIC["name"]}


@app.get("/healthz")
async def health():
    return {"status": "ok", "clinic": runtime.CLINIC["name"] if runtime.CLINIC else None,
            "practitioners_cached": len(runtime.CACHE.practitioners) if runtime.CACHE else 0}


# Serve the browser test client at "/". Mounted LAST so it doesn't shadow the API
# routes (/ws, /pms, /twilio, /clinic, /healthz).
STATIC_DIR = Path(__file__).parent.parent / "static"
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
