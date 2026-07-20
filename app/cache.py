"""In-memory reference cache + resolver.

Slow-changing reference data (practitioners, appointment types) is pulled from
the PMS API once at startup and refreshed periodically - NEVER hit per call turn.
The Resolver turns what the caller SAID ("Dr Hepburn", "check-up") into the
integer practitioner_id / Dentally reason + duration the live tools need. That is
the "names cross the boundary, IDs stay server-side" rule made concrete.

At scale this cache moves to Redis / a Postgres table fed by a sync worker +
Dentally webhooks; the interface stays the same.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from rapidfuzz import fuzz, process

logger = logging.getLogger("dental.cache")


@dataclass
class ReferenceCache:
    clinic: dict
    practitioners: list[dict] = field(default_factory=list)
    reasons: list[dict] = field(default_factory=list)

    async def refresh(self, pms) -> None:
        """Pull practitioners + appointment reasons from the PMS API into memory.
        (This is the production 'sync worker' path.)"""
        self.practitioners = await pms.list_practitioners()
        self.reasons = await pms.list_reasons()
        logger.info("Reference cache loaded from API: %d practitioners, %d reasons",
                    len(self.practitioners), len(self.reasons))

    def load_from_db(self) -> None:
        """Bootstrap the cache directly from the DB (used at startup, to avoid a
        self-HTTP call before the server is serving). Same shape as the API."""
        from sqlalchemy import select

        from .models import AppointmentReason, Practitioner, SessionLocal
        with SessionLocal() as db:
            self.practitioners = [{
                "id": p.id, "active": p.active, "role": p.role, "site_id": p.site_id,
                "user": {"first_name": p.first_name, "last_name": p.last_name, "role": p.role},
            } for p in db.scalars(select(Practitioner).where(Practitioner.active.is_(True)))]
            self.reasons = [{
                "id": r.id, "reason": r.reason, "exam": r.exam, "hygiene": r.hygiene,
                "default_duration_minutes": r.default_duration_minutes,
            } for r in db.scalars(select(AppointmentReason).where(AppointmentReason.deleted.is_(False)))]
        logger.info("Reference cache loaded from DB: %d practitioners, %d reasons",
                    len(self.practitioners), len(self.reasons))

    # ---- appointment type resolution (from clinic config) ----
    def appointment_types(self) -> list[dict]:
        return self.clinic.get("appointment_types", [])

    def resolve_appointment_type(self, spoken: str) -> dict | None:
        types = self.appointment_types()
        if not types:
            return None
        names = [t["name"] for t in types]
        match = process.extractOne(spoken or "", names, scorer=fuzz.WRatio, score_cutoff=70)
        return types[match[2]] if match else None

    # ---- clinician resolution against the cached roster ----
    def by_role(self, role: str | None) -> list[dict]:
        if not role:
            return self.practitioners
        return [p for p in self.practitioners if p.get("role") == role]

    def practitioner(self, pid: int) -> dict | None:
        return next((p for p in self.practitioners if p["id"] == pid), None)

    def name_of(self, pid: int) -> str:
        p = self.practitioner(pid) or {}
        u = p.get("user", {})
        return f"Dr {u.get('first_name','')} {u.get('last_name','')}".strip()

    def resolve_clinician(self, spoken: str | None, role: str | None = None):
        """Return (status, data). status in {'any','one','many','none'}."""
        pool = self.by_role(role)
        if not spoken or spoken.strip().lower() in {
            "any", "anyone", "any dentist", "any hygienist", "soonest",
            "first available", "no preference", "",
        }:
            return "any", pool

        choices: dict[str, dict] = {}
        for p in pool:
            u = p.get("user", {})
            full = f"{u.get('first_name', '')} {u.get('last_name', '')}".strip()
            if full:
                choices[full] = p
            if u.get("last_name"):
                choices[u["last_name"]] = p

        cleaned = spoken.lower().replace("doctor", "").replace("dr", "").strip()
        results = process.extract(cleaned, list(choices.keys()),
                                  scorer=fuzz.WRatio, score_cutoff=75, limit=5)
        matched, seen = [], set()
        for name, _score, _idx in results:
            p = choices[name]
            if p["id"] not in seen:
                seen.add(p["id"])
                matched.append(p)
        if not matched:
            return "none", pool
        if len(matched) == 1:
            return "one", matched[0]
        return "many", matched
