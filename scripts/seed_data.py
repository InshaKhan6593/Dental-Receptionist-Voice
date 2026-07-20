"""Seed a realistic, medium-scale UK dental practice into Postgres.

Run once after `docker compose up -d`:

    python -m scripts.seed_data

Creates ~12 clinicians, ~3000 patients and thousands of appointments (mostly
historical, with a lightly-booked next 3 weeks so real availability exists).
A few fixed "known" patients are added so the voice demo is repeatable.
"""
from __future__ import annotations

import random
import uuid
from datetime import date, datetime, time, timedelta

from faker import Faker

from app.models import (Appointment, AppointmentReason, Patient, Practitioner,
                        SessionLocal, Site, init_db)

fake = Faker("en_GB")
Faker.seed(42)
random.seed(42)

SITE_ID = str(uuid.uuid4())
N_PATIENTS = 3000
DENTISTS, HYGIENISTS, THERAPISTS = 8, 3, 1
WORK_START, WORK_END = 9, 17

# Dentally's fixed reason set + our per-reason default durations.
REASONS = [
    ("Exam", True, False, 15),
    ("Scale & Polish", False, True, 30),
    ("Exam + Scale & Polish", True, True, 30),
    ("Continuing Treatment", True, False, 30),
    ("Emergency", True, False, 20),
    ("Review", True, False, 10),
    ("Other", False, False, 15),
]
DUR = {r[0]: r[3] for r in REASONS}

# Fixed patients for a repeatable demo (share these when testing verification).
KNOWN = [
    ("Mr", "John", "Smith", date(1985, 4, 12), "SW1A 1AA", "07700900001"),
    ("Mrs", "Priya", "Patel", date(1990, 11, 3), "M1 2AB", "07700900002"),
    ("Mr", "Liam", "Byrne", date(1972, 7, 22), "BT1 5GS", "07700900003"),
]


def _random_slot(day: date, dur: int):
    hour = random.randint(WORK_START, WORK_END - 1)
    minute = random.choice([0, 15, 30, 45])
    s = datetime.combine(day, time(hour, minute))
    return s, s + timedelta(minutes=dur)


def seed():
    init_db()
    with SessionLocal() as db:
        for model in (Appointment, Patient, AppointmentReason, Practitioner, Site):
            db.query(model).delete()
        db.commit()

        db.add(Site(id=SITE_ID, name="Riverside Dental Practice"))
        for i, (name, exam, hyg, dur) in enumerate(REASONS):
            db.add(AppointmentReason(id=str(uuid.uuid4()), reason=name, exam=exam,
                                     hygiene=hyg, default_duration_minutes=dur, position=i))
        db.commit()   # commit the site (parent) before FK children reference it

        practitioners, pid = [], 1

        def add_prac(role):
            nonlocal pid
            p = Practitioner(id=pid, site_id=SITE_ID, role=role,
                             first_name=fake.first_name(), last_name=fake.last_name(),
                             active=True, gdc_number=str(fake.random_number(digits=6)),
                             colour=fake.hex_color())
            db.add(p)
            practitioners.append(p)
            pid += 1
            return p

        dentists = [add_prac("Dentist") for _ in range(DENTISTS)]
        hygienists = [add_prac("Hygienist") for _ in range(HYGIENISTS)]
        [add_prac("Therapist") for _ in range(THERAPISTS)]
        db.commit()

        # known patients (ids 1001..)
        pid_counter = 1000
        for title, fn, ln, dob, pc, mob in KNOWN:
            pid_counter += 1
            db.add(Patient(id=pid_counter, site_id=SITE_ID, title=title, first_name=fn,
                           last_name=ln, date_of_birth=dob, postcode=pc, mobile_phone=mob,
                           email_address=f"{fn.lower()}.{ln.lower()}@example.com",
                           dentist_id=dentists[0].id, hygienist_id=hygienists[0].id,
                           active=True))
        db.commit()

        # random patients
        patient_ids = [pid_counter]
        start_id = pid_counter + 1
        batch = []
        for i in range(N_PATIENTS):
            gid = start_id + i
            batch.append(Patient(
                id=gid, site_id=SITE_ID, title=fake.prefix(),
                first_name=fake.first_name(), last_name=fake.last_name(),
                date_of_birth=fake.date_of_birth(minimum_age=1, maximum_age=90),
                postcode=fake.postcode(),
                mobile_phone="07" + str(fake.random_number(digits=9, fix_len=True)),
                email_address=fake.email(), address_line_1=fake.street_address(),
                town=fake.city(), dentist_id=random.choice(dentists).id,
                hygienist_id=random.choice(hygienists).id, active=True))
            patient_ids.append(gid)
            if len(batch) >= 500:
                db.add_all(batch)
                db.commit()
                batch = []
        if batch:
            db.add_all(batch)
            db.commit()

        today = date.today()
        appts = []

        # history (~6000, mostly completed)
        for _ in range(6000):
            d = today - timedelta(days=random.randint(1, 180))
            if d.weekday() >= 5:
                continue
            reason = random.choice(["Exam", "Scale & Polish", "Continuing Treatment", "Review"])
            prac = random.choice(hygienists if reason == "Scale & Polish" else dentists)
            s, e = _random_slot(d, DUR[reason])
            appts.append(Appointment(
                patient_id=random.choice(patient_ids), practitioner_id=prac.id, reason=reason,
                start_time=s, finish_time=e, duration=DUR[reason],
                state=random.choice(["Completed", "Completed", "Cancelled", "Did not attend"])))

        # sparse future (next 3 weeks) so real availability exists
        for day_offset in range(1, 22):
            d = today + timedelta(days=day_offset)
            if d.weekday() >= 5:
                continue
            for prac in dentists + hygienists:
                for _ in range(random.randint(1, 4)):
                    reason = ("Scale & Polish" if prac.role == "Hygienist"
                              else random.choice(["Exam", "Continuing Treatment"]))
                    s, e = _random_slot(d, DUR[reason])
                    appts.append(Appointment(
                        patient_id=random.choice(patient_ids), practitioner_id=prac.id,
                        reason=reason, start_time=s, finish_time=e, duration=DUR[reason],
                        state="Confirmed"))
            if len(appts) >= 1000:
                db.add_all(appts)
                db.commit()
                appts = []
        if appts:
            db.add_all(appts)
            db.commit()

    print("Seed complete.")
    print(f"  practitioners : {DENTISTS} dentists, {HYGIENISTS} hygienists, {THERAPISTS} therapist")
    print(f"  patients      : {N_PATIENTS + len(KNOWN)}")
    print("  appointments  : ~6000 history + lightly-booked next 3 weeks")
    print("\nKnown test patients (use these to verify on a call):")
    for _t, fn, ln, dob, pc, _m in KNOWN:
        print(f"  {fn} {ln:8} | DOB {dob.isoformat()} | postcode {pc}")


if __name__ == "__main__":
    seed()
