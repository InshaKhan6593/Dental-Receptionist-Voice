"""LangSmith tracing: per-call voice recording + tool spans.

What the client cares about: every call becomes a LangSmith run with the CALL
RECORDING attached (caller + agent audio), plus transcript and final disposition.
Individual PMS operations are traced as 'tool' runs too.

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

try:
    from langsmith import traceable
    from langsmith.schemas import Attachment
    _LS = True
except Exception:  # pragma: no cover
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


# ------------------------------- tool spans ---------------------------------
def traced_tool(name: str):
    """Decorate a plain helper so it shows as a LangSmith 'tool' run. No-op if off."""
    def deco(fn):
        if not enabled():
            return fn
        return traceable(run_type="tool", name=name)(fn)
    return deco


def traced_session(session_id: str | None) -> dict:
    """Kwargs that thread a traced helper's run into a per-call LangSmith thread
    (grouped by session_id). Spread it into the helper call:
        await _find_patient(...args, **traced_session(state.get("call_sid")))
    Returns {} when tracing is off, so the SAME call site works whether or not the
    helper is wrapped by @traceable (the bare function has no langsmith_extra)."""
    if not enabled() or not session_id:
        return {}
    return {"langsmith_extra": {"metadata": {"session_id": session_id}}}


# ------------------------------ call recording ------------------------------
if _LS:
    # ls_modality=audio makes LangSmith render this as a voice trace.
    @traceable(run_type="chain", name="voice_call",
               metadata={"ls_modality": "audio"})
    def _log_call(clinic_id, call_sid, caller_number, disposition, verified,
                  transcript,
                  caller_audio: Attachment = None,
                  agent_audio: Attachment = None):
        # The bare `Attachment` annotation (not Optional) is what makes the SDK
        # upload these as attachments instead of serializing them into inputs.
        return {"disposition": disposition, "verified": verified,
                "transcript_chars": len(transcript or "")}


def log_call(clinic_id, call_sid, caller_number, disposition, verified,
             transcript, recorder: "CallRecorder | None") -> None:
    """Create the per-call LangSmith run with the voice recording attached."""
    if not enabled():
        return
    try:
        wavs = recorder.wavs() if recorder else {}
        kwargs = {}
        if "caller_audio" in wavs:
            kwargs["caller_audio"] = Attachment(mime_type="audio/wav", data=wavs["caller_audio"])
        if "agent_audio" in wavs:
            kwargs["agent_audio"] = Attachment(mime_type="audio/wav", data=wavs["agent_audio"])
        # session_id threads this summary together with the call's PMS tool runs;
        # ls_modality/clinic_id/caller_number are repeated here so they survive
        # whether call-time metadata merges with or replaces the decorator's.
        _log_call(clinic_id=clinic_id, call_sid=call_sid, caller_number=caller_number,
                  disposition=disposition, verified=verified, transcript=transcript,
                  langsmith_extra={"metadata": {
                      "session_id": call_sid, "ls_modality": "audio",
                      "clinic_id": clinic_id, "caller_number": caller_number,
                      "disposition": disposition}},
                  **kwargs)
    except Exception:
        logger.exception("LangSmith call logging failed (non-fatal)")
