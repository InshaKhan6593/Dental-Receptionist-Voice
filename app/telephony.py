"""Audio transcoding for telephony.

Twilio Media Streams carry G.711 mu-law at 8 kHz mono. Gemini Live wants PCM16
at 16 kHz in / 24 kHz out. `ratecv` state is threaded per-stream for clean
resampling across 20 ms frames.

NOTE: uses `audioop` (stdlib on Python <=3.12; the `audioop-lts` backport on
3.13+, declared in pyproject). Import is guarded so the backend still boots even
if the backport is missing - only the Twilio transcode path would then error.
"""
from __future__ import annotations

try:
    import audioop
except ModuleNotFoundError:  # Python 3.13+ without audioop-lts installed
    audioop = None


def _require_audioop():
    if audioop is None:
        raise RuntimeError("audioop unavailable - `pip install audioop-lts` (Python 3.13+)")


def ulaw8k_to_pcm16k(mulaw: bytes, state=None):
    """Twilio inbound: mu-law 8k -> PCM16 16k."""
    _require_audioop()
    pcm8 = audioop.ulaw2lin(mulaw, 2)
    pcm16, state = audioop.ratecv(pcm8, 2, 1, 8000, 16000, state)
    return pcm16, state


def pcm24k_to_ulaw8k(pcm24: bytes, state=None):
    """Agent outbound: PCM16 24k -> mu-law 8k."""
    _require_audioop()
    pcm8, state = audioop.ratecv(pcm24, 2, 1, 24000, 8000, state)
    return audioop.lin2ulaw(pcm8, 2), state
