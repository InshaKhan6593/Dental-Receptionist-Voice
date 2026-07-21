"""LangSmith tracing: a per-call waterfall (voice_call -> PMS tool runs +
recording), not disconnected traces.

Each call opens ONE `voice_call` run at call start (start_call); every PMS tool
run made during the call nests UNDER it via langsmith_extra["parent"], so the
call renders as a single waterfall instead of a pile of unrelated root traces
merely grouped by thread. At call end the recording (caller + agent WAVs),
transcript and disposition are attached and the parent is closed (log_call).

All LangSmith usage is guarded and best-effort - tracing must NEVER break a call.
Enable with LANGSMITH_TRACING=true and LANGSMITH_API_KEY in .env.

No `from __future__ import annotations` here: the LangSmith SDK detects
attachment parameters by comparing the *runtime* annotation to the bare
`Attachment` class; stringified or Optional[...] annotations are not matched,
and the WAVs would silently upload as inputs JSON instead of attachments.
"""
import io
import logging
import os
import wave

logger = logging.getLogger("dental.tracing")

TRACING = os.getenv("LANGSMITH_TRACING", "").lower() == "true"
PROJECT = os.getenv("LANGSMITH_PROJECT", "dental-receptionist")

try:
    from langsmith import RunTree, traceable
    from langsmith.schemas import Attachment
    _LS = True
except Exception:  # pragma: no cover
    RunTree = None
    traceable = None
    Attachment = None
    _LS = False


def enabled() -> bool:
    return TRACING and _LS


# --------------------------------- audio ------------------------------------
def pcm_to_wav(pcm: bytes, rate: int, channels: int = 1, sampwidth: int = 2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(sampwidth)
        w.setframerate(rate)
        w.writeframes(bytes(pcm))
    return buf.getvalue()


class CallRecorder:
    """Accumulates caller + agent PCM through a call; encodes WAVs at the end."""

    def __init__(self, caller_rate: int = 16000, agent_rate: int = 24000):
        self.caller = bytearray()
        self.agent = bytearray()
        self.caller_rate = caller_rate
        self.agent_rate = agent_rate

    def add_caller(self, pcm16: bytes) -> None:
        self.caller += pcm16

    def add_agent(self, pcm: bytes) -> None:
        self.agent += pcm

    def wavs(self) -> dict[str, bytes]:
        out: dict[str, bytes] = {}
        if self.caller:
            out["caller_audio"] = pcm_to_wav(self.caller, self.caller_rate)
        if self.agent:
            out["agent_audio"] = pcm_to_wav(self.agent, self.agent_rate)
        return out


# --------------------------- per-call parent run ----------------------------
# call_sid -> the open `voice_call` RunTree. Tool runs nest UNDER it so a call
# renders as one waterfall, instead of disconnected roots grouped only by thread.
_CALL_RUNS: dict = {}


def start_call(call_sid: str | None, clinic_id: str | None = None,
               caller_number: str | None = None) -> None:
    """Open the parent `voice_call` run at call start, so every PMS tool run made
    during the call nests under it. Best-effort; never raises."""
    if not enabled() or not call_sid:
        return
    try:
        run = RunTree(
            name="voice_call", run_type="chain", project_name=PROJECT,
            inputs={"caller_number": caller_number, "clinic_id": clinic_id},
            extra={"metadata": {"session_id": call_sid, "ls_modality": "audio",
                                "clinic_id": clinic_id, "caller_number": caller_number}})
        run.post()
        _CALL_RUNS[call_sid] = run
    except Exception:
        logger.exception("LangSmith start_call failed (non-fatal)")


# ------------------------------- tool spans ---------------------------------
def traced_tool(name: str):
    """Decorate a plain helper so it shows as a LangSmith 'tool' run. No-op if off."""
    def deco(fn):
        if not enabled():
            return fn
        return traceable(run_type="tool", name=name)(fn)
    return deco


def traced_session(call_sid: str | None) -> dict:
    """Kwargs that nest a traced helper's run UNDER this call's `voice_call` run,
    turning the call's PMS operations into a waterfall. Spread it into the call:
        await _find_patient(...args, **traced_session(state.get("call_sid")))
    Returns {} when tracing is off or the parent run is missing, so the SAME call
    site works whether or not the helper is wrapped by @traceable."""
    if not enabled() or not call_sid:
        return {}
    parent = _CALL_RUNS.get(call_sid)
    if parent is None:
        return {}
    return {"langsmith_extra": {"parent": parent,
                                "metadata": {"session_id": call_sid}}}


# ------------------------------ call recording ------------------------------
if _LS:
    # A leaf run carrying the call's WAVs; nested under the voice_call parent via
    # langsmith_extra["parent"]. ls_modality=audio renders it as a voice trace.
    @traceable(run_type="chain", name="call_recording",
               metadata={"ls_modality": "audio"})
    def _log_recording(clinic_id, call_sid, caller_number, disposition, verified,
                       transcript,
                       caller_audio: Attachment = None,
                       agent_audio: Attachment = None):
        # The bare `Attachment` annotation (not Optional) is what makes the SDK
        # upload these as attachments instead of serializing them into inputs.
        return {"disposition": disposition, "verified": verified,
                "transcript_chars": len(transcript or "")}


def log_call(clinic_id, call_sid, caller_number, disposition, verified,
             transcript, recorder: "CallRecorder | None") -> None:
    """Close out the call's `voice_call` run: attach the recording as a child run,
    then finalize the parent with the disposition + transcript."""
    if not enabled():
        return
    parent = _CALL_RUNS.pop(call_sid, None)
    try:
        wavs = recorder.wavs() if recorder else {}
        # Pass attachments as (mime_type, bytes) TUPLES, not Attachment instances:
        # RunTree.attachments accepts the tuple form in every langsmith we target,
        # whereas an Attachment instance trips a pydantic "call" validator in newer
        # versions, and a None value (one side silent) fails validation outright.
        # So always supply a valid tuple, falling back to an empty-but-valid WAV
        # when a side never spoke (e.g. an abandoned call).
        caller_audio = ("audio/wav", wavs.get("caller_audio") or pcm_to_wav(b"", 16000))
        agent_audio = ("audio/wav", wavs.get("agent_audio") or pcm_to_wav(b"", 24000))
        extra = {"metadata": {
            "session_id": call_sid, "ls_modality": "audio", "clinic_id": clinic_id,
            "caller_number": caller_number, "disposition": disposition}}
        # Nest the recording under the call's parent run on the normal path; if the
        # parent is missing (tracing raced / start failed) it stands alone so the
        # recording is never lost.
        if parent is not None:
            extra["parent"] = parent
        _log_recording(clinic_id=clinic_id, call_sid=call_sid, caller_number=caller_number,
                       disposition=disposition, verified=verified, transcript=transcript,
                       caller_audio=caller_audio, agent_audio=agent_audio,
                       langsmith_extra=extra)
    except Exception:
        logger.exception("LangSmith call logging failed (non-fatal)")
    finally:
        if parent is not None:
            try:
                parent.end(outputs={"disposition": disposition, "verified": verified,
                                    "transcript": transcript})
                parent.patch()
            except Exception:
                logger.exception("LangSmith parent finalize failed (non-fatal)")
