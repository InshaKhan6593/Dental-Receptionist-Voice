"""ADK tools = the agent's real actions. Rule: the model passes NAMES (what the
caller said); code resolves names -> IDs against the cache and calls the live PMS.
Guardrails (verify-before-book) are enforced here AND in a before_tool_callback.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, time, timedelta

from google.adk.tools import ToolContext

from . import runtime
from .models import CallbackRequest, SessionLocal
from .tracing import traced_session, traced_tool

logger = logging.getLogger("dental.tools")

SLOT_BATCH = 6        # options offered per turn (voice-friendly)
SLOT_QUEUE_MAX = 24   # slots kept in state for "anything later?" follow-ups
MAX_DAY_WINDOWS = 5   # parallel per-day PMS fetches per filtered search
MORNING_END = time(12, 0)

_DAY_NUMBERS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}
_WEEKDAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday",
                  "saturday", "sunday"]


def _human(iso: str) -> str:
    dt = datetime.fromisoformat(iso)
    return dt.strftime("%A %d %b at %I:%M %p").replace(" 0", " ")


def _opening_windows(clinic: dict) -> dict[int, tuple[time, time]]:
    """Weekday -> (open, close) from the clinic YAML; closed days are absent."""
    out: dict[int, tuple[time, time]] = {}
    for day, hours in (clinic.get("opening_hours") or {}).items():
        wd = _DAY_NUMBERS.get(str(day).lower())
        if wd is None or not isinstance(hours, str) or "-" not in hours:
            continue
        try:
            lo, hi = (time.fromisoformat(p.strip()) for p in hours.split("-", 1))
        except ValueError:
            continue
        if lo < hi:
            out[wd] = (lo, hi)
    return out


def _parse_days(raw: str) -> tuple[set[int] | None, list[str]]:
    """'monday, fri' -> ({0, 4}, []). None = no day preference."""
    tokens = [t for t in re.split(r"[,\s/]+", (raw or "").strip().lower()) if t]
    if not tokens or "any" in tokens:
        return None, []
    days, bad = set(), []
    for t in tokens:
        wd = _DAY_NUMBERS.get(t)
        bad.append(t) if wd is None else days.add(wd)
    return (days or None), bad


def _clip_hours(lo: time, hi: time, time_of_day: str) -> tuple[time, time] | None:
    tod = (time_of_day or "any").strip().lower()
    if tod in ("morning", "am"):
        hi = min(hi, MORNING_END)
    elif tod in ("afternoon", "pm", "evening"):
        lo = max(lo, MORNING_END)
    return (lo, hi) if lo < hi else None


def _offer_next_batch(state) -> list[dict]:
    """Move the next SLOT_BATCH slots from the queue into offered_slots, assigning
    stable slot_ids. Previously offered slots stay bookable across batches."""
    queue = list(state.get("slot_queue") or [])
    batch, state["slot_queue"] = queue[:SLOT_BATCH], queue[SLOT_BATCH:]
    seq = state.get("slot_seq", 0)
    offered = list(state.get("offered_slots") or [])
    out = []
    for s in batch:
        s = dict(s, slot_id=seq)
        seq += 1
        offered.append(s)
        out.append(s)
    state["slot_seq"] = seq
    state["offered_slots"] = offered
    return out


def _spoken(slots: list[dict]) -> list[dict]:
    return [{"slot_id": s["slot_id"], "when": _human(s["start_time"]),
             "clinician": s["clinician"]} for s in slots]


# ---- traced PMS helpers (plain funcs -> LangSmith 'tool' runs) ----
@traced_tool("pms.find_patient")
async def _find_patient(last_name, date_of_birth, postcode):
    return await runtime.PMS.find_patient(last_name, date_of_birth, postcode)


@traced_tool("pms.availability")
async def _availability(pids, start, finish, duration):
    return await runtime.PMS.availability(pids, start, finish, duration)


@traced_tool("pms.book")
async def _book(**kw):
    return await runtime.PMS.book(**kw)


@traced_tool("pms.list_appointments")
async def _list_appointments(patient_id):
    return await runtime.PMS.list_patient_appointments(patient_id)


@traced_tool("pms.cancel")
async def _cancel(appointment_id, reason):
    return await runtime.PMS.cancel(appointment_id, reason)


@traced_tool("pms.reschedule")
async def _reschedule(appointment_id, start_time, finish_time):
    return await runtime.PMS.reschedule(appointment_id, start_time, finish_time)


# ------------------------------- the tools ----------------------------------
async def verify_patient(last_name: str, date_of_birth: str, postcode: str,
                         tool_context: ToolContext) -> dict:
    """Verify an existing patient against the practice records. Call this once you
    have collected all three details from the caller.

    Args:
        last_name: The caller's surname.
        date_of_birth: Date of birth in YYYY-MM-DD format.
        postcode: The caller's postcode.
    """
    state = tool_context.state
    attempts = state.get("verify_attempts", 0) + 1
    state["verify_attempts"] = attempts
    max_attempts = runtime.CLINIC["verification"].get("max_attempts", 3)
    try:
        matches = await _find_patient(last_name, date_of_birth, postcode,
                                      **traced_session(state.get("call_sid")))
    except Exception:
        logger.exception("verify_patient failed")
        return {"status": "error", "message": "records system is unavailable"}

    if len(matches) == 1:
        p = matches[0]
        state["verified"] = True
        state["patient_id"] = p["id"]
        state["patient_name"] = f"{p['first_name']} {p['last_name']}"
        state["caller_type"] = "existing"
        result = {"status": "verified", "first_name": p["first_name"]}
        # Their assigned dentist, so the agent can offer "with Dr X as usual?"
        if p.get("dentist_id") and runtime.CACHE.practitioner(p["dentist_id"]):
            result["usual_dentist"] = runtime.CACHE.name_of(p["dentist_id"])
            state["usual_dentist"] = result["usual_dentist"]
        return result

    remaining = max_attempts - attempts
    if remaining <= 0:
        return {"status": "locked",
                "message": "verification failed - offer a callback or transfer"}
    return {"status": "no_match", "attempts_remaining": remaining}


async def get_availability(appointment_type: str, clinician: str = "any",
                           preferred_days: str = "", time_of_day: str = "any",
                           from_date: str = "", days_ahead: int = 14,
                           tool_context: ToolContext = None) -> dict:
    """Search appointment slots matching the caller's preferences; offers up to 6.
    Starts a NEW search - call it again whenever a preference changes. If the
    caller just wants other/later options from the same search, call get_more_slots.

    Args:
        appointment_type: e.g. "check-up", "hygiene", "emergency", "new patient exam".
        clinician: a clinician the caller asked for by name, or "any".
        preferred_days: weekday(s) the caller prefers, comma-separated, e.g.
            "monday" or "monday,friday". Empty means no preference.
        time_of_day: "morning", "afternoon" or "any".
        from_date: earliest acceptable date as YYYY-MM-DD (e.g. caller says "the
            week after next"). Empty means from today.
        days_ahead: how many days to search from the start date (default 14).
    """
    cache = runtime.CACHE
    at = cache.resolve_appointment_type(appointment_type)
    if not at:
        return {"status": "unknown_type",
                "available_types": [t["name"] for t in cache.appointment_types()]}

    role = at.get("role")
    status, data = cache.resolve_clinician(clinician, role=role)
    if status == "many":
        return {"status": "disambiguate", "options": [cache.name_of(p["id"]) for p in data]}
    if status == "none":
        return {"status": "clinician_not_found",
                "available": [cache.name_of(p["id"]) for p in cache.by_role(role)]}

    practitioners = data if isinstance(data, list) else [data]
    pids = [p["id"] for p in practitioners]

    wanted_days, bad_days = _parse_days(preferred_days)
    if bad_days and wanted_days is None:
        return {"status": "unknown_day",
                "message": f"could not understand day(s): {', '.join(bad_days)}"}

    earliest = datetime.now() + timedelta(hours=1)
    start_day = earliest.date()
    if from_date:
        try:
            start_day = max(date.fromisoformat(from_date.strip()), start_day)
        except ValueError:
            return {"status": "bad_date", "message": "from_date must be YYYY-MM-DD"}

    opening = _opening_windows(runtime.CLINIC)
    if wanted_days is not None and opening and not (wanted_days & set(opening)):
        return {"status": "closed_days",
                "open_days": [_WEEKDAY_NAMES[d] for d in sorted(opening)]}

    days_ahead = max(1, min(days_ahead or 14, 60))
    duration = at["duration_minutes"]
    tod = (time_of_day or "any").strip().lower()
    filtered = wanted_days is not None or tod in ("morning", "am", "afternoon", "pm", "evening")
    sx = traced_session(tool_context.state.get("call_sid"))

    try:
        if not filtered:
            # Fast path (no preferences): one PMS call, earliest-first.
            start = max(datetime.combine(start_day, time.min), earliest)
            slots = await _availability(pids, start.isoformat(),
                                        (start + timedelta(days=days_ahead)).isoformat(),
                                        duration, **sx)
        else:
            # Preferences become per-day time windows (clinic opening hours,
            # clipped to the preferred days / time of day), fetched in parallel:
            # correct against any PMS that takes a start/finish range, and still
            # a single round-trip of latency.
            windows = []
            for i in range(days_ahead):
                day = start_day + timedelta(days=i)
                hours = opening.get(day.weekday())
                if not hours:
                    continue
                if wanted_days is not None and day.weekday() not in wanted_days:
                    continue
                clipped = _clip_hours(*hours, tod)
                if not clipped:
                    continue
                ws = datetime.combine(day, clipped[0])
                we = datetime.combine(day, clipped[1])
                if we <= earliest:
                    continue
                windows.append((max(ws, earliest), we))
                if len(windows) >= MAX_DAY_WINDOWS:
                    break
            if not windows:
                return {"status": "no_slots"}
            results = await asyncio.gather(
                *(_availability(pids, ws.isoformat(), we.isoformat(), duration, **sx)
                  for ws, we in windows),
                return_exceptions=True)
            slots, failures = [], 0
            for r in results:
                if isinstance(r, BaseException):
                    failures += 1
                else:
                    slots.extend(r)
            if failures == len(results):
                logger.error("get_availability: all %d window fetches failed", failures)
                return {"status": "error"}
            if failures:
                logger.warning("get_availability: %d/%d window fetches failed",
                               failures, len(results))
            seen: set = set()
            deduped = []
            for s in sorted(slots, key=lambda s: s["start_time"]):
                key = (s["practitioner_id"], s["start_time"])
                if key not in seen:
                    seen.add(key)
                    deduped.append(s)
            slots = deduped
    except Exception:
        logger.exception("get_availability failed")
        return {"status": "error"}

    if status == "any":
        # No clinician preference: offer varied TIMES, not the same time with
        # every clinician ("9:00, 9:00, 9:15..." reads terribly on a call).
        seen_times: set = set()
        slots = [s for s in slots
                 if not (s["start_time"] in seen_times or seen_times.add(s["start_time"]))]

    state = tool_context.state
    state["slot_queue"] = [{
        "start_time": s["start_time"], "finish_time": s["finish_time"],
        "practitioner_id": s["practitioner_id"], "reason": at["dentally_reason"],
        "clinician": cache.name_of(s["practitioner_id"]),
    } for s in slots[:SLOT_QUEUE_MAX]]
    state["intent"] = state.get("intent") or "book"

    offered = _offer_next_batch(state)
    if not offered:
        return {"status": "no_slots"}
    return {"status": "ok", "slots": _spoken(offered),
            "more_available": bool(state.get("slot_queue"))}


async def get_more_slots(tool_context: ToolContext) -> dict:
    """Offer the next batch of slots from the current search, when the caller asks
    for other or later options. Instant (no records-system call). If the caller
    changes any preference, call get_availability again instead.
    """
    state = tool_context.state
    if not state.get("slot_queue"):
        return {"status": "no_more_slots",
                "message": "no further slots in this search - offer different days, "
                           "times, clinicians or a later from_date"}
    offered = _offer_next_batch(state)
    return {"status": "ok", "slots": _spoken(offered),
            "more_available": bool(state.get("slot_queue"))}


async def book_appointment(slot_id: int, tool_context: ToolContext) -> dict:
    """Book one of the slots previously offered by get_availability.

    Args:
        slot_id: The slot_id of the slot the caller chose.
    """
    state = tool_context.state
    if not state.get("verified"):
        return {"status": "not_verified", "message": "verify the patient before booking"}
    slot = next((o for o in state.get("offered_slots", []) if o["slot_id"] == slot_id), None)
    if not slot:
        return {"status": "invalid_slot"}
    try:
        appt = await _book(patient_id=state["patient_id"],
                           practitioner_id=slot["practitioner_id"], reason=slot["reason"],
                           start_time=slot["start_time"], finish_time=slot["finish_time"],
                           **traced_session(state.get("call_sid")))
    except Exception:
        logger.exception("book_appointment failed")
        return {"status": "error"}
    state["appointment_id"] = appt["id"]
    state["disposition"] = "booked"
    return {"status": "booked", "appointment_id": appt["id"],
            "when": _human(slot["start_time"]), "clinician": slot["clinician"]}


async def list_my_appointments(tool_context: ToolContext) -> dict:
    """List the verified patient's upcoming appointments (for reschedule/cancel)."""
    state = tool_context.state
    if not state.get("verified"):
        return {"status": "not_verified"}
    try:
        appts = await _list_appointments(state["patient_id"],
                                         **traced_session(state.get("call_sid")))
    except Exception:
        logger.exception("list_my_appointments failed")
        return {"status": "error"}
    my = [{
        "ref": i, "id": a["id"], "when": _human(a["start_time"]), "reason": a["reason"],
        "clinician": runtime.CACHE.name_of(a["practitioner_id"]),
    } for i, a in enumerate(appts)]
    state["my_appointments"] = my
    return {"status": "ok",
            "appointments": [{"ref": m["ref"], "when": m["when"], "reason": m["reason"]} for m in my]}


async def cancel_appointment(ref: int, reason: str = "",
                             tool_context: ToolContext = None) -> dict:
    """Cancel one of the patient's upcoming appointments (call list_my_appointments first).

    Args:
        ref: The ref of the appointment to cancel.
        reason: Short cancellation reason.
    """
    state = tool_context.state
    if not state.get("verified"):
        return {"status": "not_verified"}
    m = next((x for x in state.get("my_appointments", []) if x["ref"] == ref), None)
    if not m:
        return {"status": "invalid_ref"}
    try:
        await _cancel(m["id"], reason, **traced_session(state.get("call_sid")))
    except Exception:
        logger.exception("cancel_appointment failed")
        return {"status": "error"}
    state["disposition"] = "cancelled"
    return {"status": "cancelled", "when": m["when"]}


async def reschedule_appointment(ref: int, slot_id: int,
                                 tool_context: ToolContext = None) -> dict:
    """Move an existing appointment to a new slot (call list_my_appointments and
    get_availability first).

    Args:
        ref: The ref of the existing appointment.
        slot_id: The slot_id of the new slot the caller chose.
    """
    state = tool_context.state
    if not state.get("verified"):
        return {"status": "not_verified"}
    m = next((x for x in state.get("my_appointments", []) if x["ref"] == ref), None)
    slot = next((o for o in state.get("offered_slots", []) if o["slot_id"] == slot_id), None)
    if not m or not slot:
        return {"status": "invalid"}
    try:
        await _reschedule(m["id"], slot["start_time"], slot["finish_time"],
                          **traced_session(state.get("call_sid")))
    except Exception:
        logger.exception("reschedule_appointment failed")
        return {"status": "error"}
    state["disposition"] = "rescheduled"
    return {"status": "rescheduled", "when": _human(slot["start_time"])}


async def create_callback_request(reason: str, callback_number: str = "",
                                  best_time: str = "",
                                  tool_context: ToolContext = None) -> dict:
    """Log a callback for the practice when a request can't be completed now
    (no slot, records unavailable, out of scope, caller unsure).

    Args:
        reason: Why a callback is needed / what the caller wants.
        callback_number: Best number to reach the caller (defaults to caller ID).
        best_time: When to call back, if given.
    """
    state = tool_context.state
    with SessionLocal() as db:
        db.add(CallbackRequest(
            call_sid=state.get("call_sid"), clinic_id=runtime.CLINIC["clinic_id"],
            patient_name=state.get("patient_name"),
            callback_number=callback_number or state.get("caller_number"),
            reason=reason, best_time=best_time))
        db.commit()
    state["disposition"] = "callback"
    return {"status": "callback_logged"}


def transfer_to_human(reason: str, tool_context: ToolContext) -> dict:
    """Escalate to a person at the practice (emergency, caller asks, verification
    failed, or out of scope).

    Args:
        reason: Why the call is being transferred.
    """
    state = tool_context.state
    state["escalated"] = True
    state["disposition"] = "transferred"
    try:
        tool_context.actions.escalate = True   # telephony layer reads this to <Dial>
    except Exception:
        pass
    return {"status": "transferring",
            "number": runtime.CLINIC["escalation"]["transfer_number"]}


TOOLS = [
    verify_patient, get_availability, get_more_slots, book_appointment,
    list_my_appointments, reschedule_appointment, cancel_appointment,
    create_callback_request, transfer_to_human,
]
