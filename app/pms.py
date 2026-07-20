"""Mock Dentally REST API + the PMSClient the agent tools call.

This deliberately mirrors the real Dentally API shapes and endpoints
(https://developer.dentally.co), so the tool + client code is identical against
the real thing later - you only change PMS_BASE_URL and add the real token.

Boundary rule made concrete:
  * volatile data (patients, availability, appointments) is served LIVE here;
  * slow-changing reference data (practitioners, appointment_reasons) is synced
    FROM these endpoints into the in-memory cache (see cache.py), never hit
    per-turn.
The router is mounted under /pms/v1.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

import httpx
from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from .config import PMS_BASE_URL
from .models import (Appointment, AppointmentReason, Patient, Practitioner,
                     SessionLocal)

# ---- Practitioner working pattern (used to compute availability) ----
WORK_START = time(9, 0)
WORK_END = time(17, 0)
LUNCH_START = time(13, 0)
LUNCH_END = time(14, 0)
SLOT_STEP_MINUTES = 15   # granularity of offered start times

router = APIRouter(prefix="/pms/v1", tags=["mock-dentally"])


# ------------------------------ serializers ---------------------------------
def _practitioner_dict(p: Practitioner) -> dict:
    return {
        "id": p.id, "active": p.active, "colour": p.colour, "site_id": p.site_id,
        "role": p.role,
        "user": {"first_name": p.first_name, "last_name": p.last_name, "role": p.role},
    }


def _reason_dict(r: AppointmentReason) -> dict:
    return {
        "id": r.id, "reason": r.reason, "exam": r.exam, "hygiene": r.hygiene,
        "default_duration_minutes": r.default_duration_minutes,
        "position": r.position, "deleted": r.deleted,
    }


def _patient_dict(p: Patient) -> dict:
    return {
        "id": p.id, "title": p.title, "first_name": p.first_name,
        "last_name": p.last_name, "date_of_birth": p.date_of_birth.isoformat(),
        "postcode": p.postcode, "mobile_phone": p.mobile_phone,
        "email_address": p.email_address, "nhs_number": p.nhs_number,
        "dentist_id": p.dentist_id, "hygienist_id": p.hygienist_id,
        "active": p.active, "site_id": p.site_id,
    }


def _appointment_dict(a: Appointment) -> dict:
    return {
        "id": a.id, "patient_id": a.patient_id, "practitioner_id": a.practitioner_id,
        "reason": a.reason, "start_time": a.start_time.isoformat(),
        "finish_time": a.finish_time.isoformat(), "duration": a.duration,
        "state": a.state, "notes": a.notes, "booked_via_api": a.booked_via_api,
    }


# ------------------------------- endpoints ----------------------------------
@router.get("/practitioners")
def list_practitioners():
    with SessionLocal() as db:
        rows = db.scalars(select(Practitioner).where(Practitioner.active.is_(True))).all()
        return {"practitioners": [_practitioner_dict(p) for p in rows]}


@router.get("/appointment_reasons")
def list_reasons():
    with SessionLocal() as db:
        rows = db.scalars(
            select(AppointmentReason).where(AppointmentReason.deleted.is_(False))
        ).all()
        return {"appointment_reasons": [_reason_dict(r) for r in rows]}


@router.get("/patients")
def search_patients(last_name: str | None = None, date_of_birth: str | None = None,
                    postcode: str | None = None):
    """Knowledge-based lookup used for verification. Matches are ANDed.
    Postcode is compared space-insensitively so "SW1A 1AA" == "sw1a1aa"."""
    with SessionLocal() as db:
        stmt = select(Patient).where(Patient.active.is_(True))
        if last_name:
            stmt = stmt.where(Patient.last_name.ilike(last_name.strip()))
        if date_of_birth:
            try:
                stmt = stmt.where(Patient.date_of_birth == date.fromisoformat(date_of_birth))
            except ValueError:
                return {"patients": []}   # malformed DOB -> no match (not a 500)
        rows = db.scalars(stmt.limit(25)).all()
        if postcode:
            norm = postcode.replace(" ", "").upper()
            rows = [p for p in rows if (p.postcode or "").replace(" ", "").upper() == norm]
        return {"patients": [_patient_dict(p) for p in rows[:10]]}


def _free_slots_for_day(db, practitioner_id: int, day: date, duration: int):
    """Working window minus lunch minus booked appointments -> free slot starts."""
    booked = db.scalars(
        select(Appointment).where(
            Appointment.practitioner_id == practitioner_id,
            Appointment.state != "Cancelled",
            Appointment.start_time >= datetime.combine(day, time.min),
            Appointment.start_time <= datetime.combine(day, time.max),
        )
    ).all()
    busy = [(a.start_time, a.finish_time) for a in booked]
    busy.append((datetime.combine(day, LUNCH_START), datetime.combine(day, LUNCH_END)))

    slots = []
    cursor = datetime.combine(day, WORK_START)
    end = datetime.combine(day, WORK_END)
    step = timedelta(minutes=SLOT_STEP_MINUTES)
    dur = timedelta(minutes=duration)
    while cursor + dur <= end:
        candidate_end = cursor + dur
        overlap = any(cursor < b_end and candidate_end > b_start for b_start, b_end in busy)
        if not overlap:
            slots.append((cursor, candidate_end))
        cursor += step
    return slots


@router.get("/appointments/availability")
def availability(practitioner_ids: list[int] = Query(...), start_time: str = Query(...),
                 finish_time: str = Query(...), duration: int = Query(15)):
    start = datetime.fromisoformat(start_time.replace("Z", ""))
    finish = datetime.fromisoformat(finish_time.replace("Z", ""))
    out = []
    with SessionLocal() as db:
        day = start.date()
        while day <= finish.date():
            if day.weekday() < 5:  # Mon-Fri
                for pid in practitioner_ids:
                    for s, e in _free_slots_for_day(db, pid, day, duration):
                        if start <= s <= finish:
                            out.append({
                                "practitioner_id": pid,
                                "start_time": s.isoformat(),
                                "finish_time": e.isoformat(),
                                "available_duration": duration,
                            })
            day += timedelta(days=1)
    out.sort(key=lambda x: x["start_time"])
    return {"availability": out[:40]}


@router.get("/appointments")
def list_appointments(patient_id: int = Query(...), upcoming: bool = True):
    with SessionLocal() as db:
        stmt = select(Appointment).where(
            Appointment.patient_id == patient_id,
            Appointment.state != "Cancelled",
        )
        if upcoming:
            stmt = stmt.where(Appointment.start_time >= datetime.now())
        rows = db.scalars(stmt.order_by(Appointment.start_time)).all()
        return {"appointments": [_appointment_dict(a) for a in rows]}


@router.post("/appointments")
def create_appointment(payload: dict):
    a = payload.get("appointment", payload)
    start = datetime.fromisoformat(a["start_time"].replace("Z", ""))
    finish = datetime.fromisoformat(a["finish_time"].replace("Z", ""))
    with SessionLocal() as db:
        clash = db.scalars(select(Appointment).where(
            Appointment.practitioner_id == a["practitioner_id"],
            Appointment.state != "Cancelled",
            Appointment.start_time < finish,
            Appointment.finish_time > start,
        )).first()
        if clash:
            raise HTTPException(status_code=422, detail="slot no longer available")
        appt = Appointment(
            patient_id=a.get("patient_id"),
            practitioner_id=a["practitioner_id"],
            reason=a.get("reason", "Exam"),
            start_time=start, finish_time=finish,
            duration=int((finish - start).total_seconds() // 60),
            state=a.get("state", "Confirmed"),
            notes=a.get("notes"),
            booked_via_api=True,
        )
        db.add(appt)
        db.commit()
        db.refresh(appt)
        return {"appointment": _appointment_dict(appt)}


@router.get("/appointments/{appointment_id}")
def get_appointment(appointment_id: int):
    with SessionLocal() as db:
        appt = db.get(Appointment, appointment_id)
        if not appt:
            raise HTTPException(404, "not found")
        return {"appointment": _appointment_dict(appt)}


@router.patch("/appointments/{appointment_id}")
def update_appointment(appointment_id: int, payload: dict):
    a = payload.get("appointment", payload)
    with SessionLocal() as db:
        appt = db.get(Appointment, appointment_id)
        if not appt:
            raise HTTPException(404, "not found")
        if a.get("start_time"):
            appt.start_time = datetime.fromisoformat(a["start_time"].replace("Z", ""))
        if a.get("finish_time"):
            appt.finish_time = datetime.fromisoformat(a["finish_time"].replace("Z", ""))
            appt.duration = int((appt.finish_time - appt.start_time).total_seconds() // 60)
        if a.get("state"):
            appt.state = a["state"]
        db.commit()
        db.refresh(appt)
        return {"appointment": _appointment_dict(appt)}


@router.delete("/appointments/{appointment_id}")
def delete_appointment(appointment_id: int, reason: str | None = None):
    with SessionLocal() as db:
        appt = db.get(Appointment, appointment_id)
        if not appt:
            raise HTTPException(404, "not found")
        appt.state = "Cancelled"
        appt.cancellation_reason = reason
        db.commit()
        db.refresh(appt)
        return {"appointment": _appointment_dict(appt)}


# ------------------------------ PMS client ----------------------------------
class PMSClient:
    """What the agent tools call. Talks HTTP to PMS_BASE_URL (the mock now, real
    Dentally later). Every method returns Dentally-shaped dicts."""

    def __init__(self, base_url: str = PMS_BASE_URL, token: str | None = None):
        self.base_url = base_url.rstrip("/")
        headers = {"User-Agent": "dental-receptionist/0.1"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=10.0)

    async def find_patient(self, last_name, date_of_birth, postcode):
        r = await self._client.get("/patients", params={
            "last_name": last_name, "date_of_birth": date_of_birth, "postcode": postcode})
        r.raise_for_status()
        return r.json().get("patients", [])

    async def list_practitioners(self):
        r = await self._client.get("/practitioners")
        r.raise_for_status()
        return r.json().get("practitioners", [])

    async def list_reasons(self):
        r = await self._client.get("/appointment_reasons")
        r.raise_for_status()
        return r.json().get("appointment_reasons", [])

    async def list_patient_appointments(self, patient_id):
        r = await self._client.get("/appointments",
                                   params={"patient_id": patient_id, "upcoming": True})
        r.raise_for_status()
        return r.json().get("appointments", [])

    async def availability(self, practitioner_ids, start_time, finish_time, duration):
        params = [("practitioner_ids", pid) for pid in practitioner_ids]
        params += [("start_time", start_time), ("finish_time", finish_time),
                   ("duration", duration)]
        r = await self._client.get("/appointments/availability", params=params)
        r.raise_for_status()
        return r.json().get("availability", [])

    async def book(self, patient_id, practitioner_id, reason, start_time, finish_time):
        r = await self._client.post("/appointments", json={"appointment": {
            "patient_id": patient_id, "practitioner_id": practitioner_id,
            "reason": reason, "start_time": start_time, "finish_time": finish_time,
            "state": "Confirmed"}})
        r.raise_for_status()
        return r.json()["appointment"]

    async def reschedule(self, appointment_id, start_time, finish_time):
        r = await self._client.patch(f"/appointments/{appointment_id}", json={"appointment": {
            "start_time": start_time, "finish_time": finish_time}})
        r.raise_for_status()
        return r.json()["appointment"]

    async def cancel(self, appointment_id, reason=None):
        r = await self._client.request("DELETE", f"/appointments/{appointment_id}",
                                       params={"reason": reason})
        r.raise_for_status()
        return r.json()["appointment"]

    async def aclose(self):
        await self._client.aclose()
