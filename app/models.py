"""SQLAlchemy models. One Postgres database, two logical groups:

  PMS tables  - shaped like the Dentally API objects (sites, practitioners,
                appointment_reasons, patients, appointments). This stands in for
                the clinic's practice-management system. In production these live
                behind Dentally's REST API; here we own them so the demo is free,
                deterministic and safe (no real patient data).

  App tables  - our own records: call_logs and callback_requests.

ADK session/message-persistence tables are created automatically by
DatabaseSessionService against this same database.
"""
from datetime import datetime

from sqlalchemy import (Boolean, Column, Date, DateTime, ForeignKey, Integer,
                        String, Text, create_engine)
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import DATABASE_URL

engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
Base = declarative_base()


# --------------------------- PMS (Dentally-shaped) ---------------------------
class Site(Base):
    __tablename__ = "pms_sites"
    id = Column(String, primary_key=True)            # uuid
    name = Column(String, nullable=False)


class Practitioner(Base):
    __tablename__ = "pms_practitioners"
    id = Column(Integer, primary_key=True)           # Dentally practitioner id
    site_id = Column(String, ForeignKey("pms_sites.id"))
    role = Column(String, nullable=False)            # Dentist / Hygienist / Therapist
    first_name = Column(String, nullable=False)
    last_name = Column(String, nullable=False)
    active = Column(Boolean, default=True)
    gdc_number = Column(String)
    colour = Column(String)


class AppointmentReason(Base):
    __tablename__ = "pms_appointment_reasons"
    id = Column(String, primary_key=True)            # uuid
    reason = Column(String, nullable=False)          # "Exam", "Scale & Polish", ...
    exam = Column(Boolean, default=False)
    hygiene = Column(Boolean, default=False)
    default_duration_minutes = Column(Integer, default=15)  # our addition (clinic config)
    position = Column(Integer, default=0)
    deleted = Column(Boolean, default=False)


class Patient(Base):
    __tablename__ = "pms_patients"
    id = Column(Integer, primary_key=True)
    site_id = Column(String, ForeignKey("pms_sites.id"))
    title = Column(String)
    first_name = Column(String, nullable=False)
    last_name = Column(String, nullable=False)
    date_of_birth = Column(Date, nullable=False)
    postcode = Column(String)
    mobile_phone = Column(String)
    email_address = Column(String)
    nhs_number = Column(String)
    address_line_1 = Column(String)
    town = Column(String)
    county = Column(String)
    dentist_id = Column(Integer, ForeignKey("pms_practitioners.id"))
    hygienist_id = Column(Integer, ForeignKey("pms_practitioners.id"))
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Appointment(Base):
    __tablename__ = "pms_appointments"
    id = Column(Integer, primary_key=True, autoincrement=True)
    patient_id = Column(Integer, ForeignKey("pms_patients.id"))
    practitioner_id = Column(Integer, ForeignKey("pms_practitioners.id"), nullable=False)
    reason = Column(String, nullable=False)
    start_time = Column(DateTime, nullable=False)
    finish_time = Column(DateTime, nullable=False)
    duration = Column(Integer, nullable=False)       # minutes
    state = Column(String, default="Pending")        # Pending/Confirmed/.../Cancelled
    notes = Column(Text)
    booked_via_api = Column(Boolean, default=False)  # True when the agent books it
    cancellation_reason = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


# ------------------------------- App tables ---------------------------------
class CallLog(Base):
    __tablename__ = "call_logs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    call_sid = Column(String, index=True)
    clinic_id = Column(String, index=True)
    caller_number = Column(String)
    intent = Column(String)
    caller_type = Column(String)
    verified = Column(Boolean, default=False)
    patient_id = Column(Integer)
    disposition = Column(String)    # booked/callback/transferred/emergency/abandoned
    escalated = Column(Boolean, default=False)
    transcript = Column(Text)
    started_at = Column(DateTime, default=datetime.utcnow)
    ended_at = Column(DateTime)


class CallbackRequest(Base):
    __tablename__ = "callback_requests"
    id = Column(Integer, primary_key=True, autoincrement=True)
    call_sid = Column(String, index=True)
    clinic_id = Column(String, index=True)
    patient_name = Column(String)
    callback_number = Column(String)
    reason = Column(String)
    best_time = Column(String)
    status = Column(String, default="open")
    created_at = Column(DateTime, default=datetime.utcnow)


def init_db():
    """Create all PMS + app tables. ADK session tables are created separately."""
    Base.metadata.create_all(engine)
