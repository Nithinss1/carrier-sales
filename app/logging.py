from uuid import uuid4
from datetime import datetime
from typing import Any, Dict, Optional, Literal
from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import create_engine, text
import os, json

router = APIRouter()

DB_URL = os.getenv("DATABASE_URL", "sqlite:///./data.db")
engine = create_engine(DB_URL, connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {})

# --- very small schema (2 tables) ---
with engine.begin() as cx:
    cx.execute(text("""
    CREATE TABLE IF NOT EXISTS sessions(
      session_id TEXT PRIMARY KEY,
      created_at TEXT,
      mc TEXT,
      eligible INTEGER,
      tier TEXT,
      outcome TEXT,
      sentiment TEXT,
      final_rate INTEGER,
      listed_rate INTEGER,
      miles INTEGER,
      load_id TEXT,
      origin TEXT,
      destination TEXT,
      equipment_type TEXT,
      rounds INTEGER DEFAULT 0
    )"""))
    cx.execute(text("""
    CREATE TABLE IF NOT EXISTS events(
      id TEXT PRIMARY KEY,
      session_id TEXT,
      ts TEXT,
      event_type TEXT,
      payload TEXT
    )"""))

class StartSessionIn(BaseModel):
    call_id: Optional[str] = None
    mc: Optional[str] = None

class StartSessionOut(BaseModel):
    session_id: str

@router.post("/session/start", response_model=StartSessionOut)
def start_session(body: StartSessionIn):
    sid = body.call_id or str(uuid4())
    now = datetime.utcnow().isoformat()
    with engine.begin() as cx:
        cx.execute(text("INSERT OR IGNORE INTO sessions(session_id, created_at, mc) VALUES (:sid, :now, :mc)"),
                   {"sid": sid, "now": now, "mc": body.mc})
    return StartSessionOut(session_id=sid)

class LogEventIn(BaseModel):
    session_id: str
    event_type: Literal[
        "verify_result","loads_pitched","negotiation_round","outcome","sentiment"
    ]
    payload: Dict[str, Any] = Field(default_factory=dict)

@router.post("/events")
def log_event(ev: LogEventIn):
    now = datetime.utcnow().isoformat()
    with engine.begin() as cx:
        cx.execute(text("INSERT INTO events(id, session_id, ts, event_type, payload) VALUES (:id,:sid,:ts,:t,:p)"),
                   {"id": str(uuid4()), "sid": ev.session_id, "ts": now, "t": ev.event_type, "p": json.dumps(ev.payload)})
        # light denormalization for dashboard speed
        if ev.event_type == "verify_result":
            cx.execute(text("""UPDATE sessions SET eligible=:e, tier=:tier WHERE session_id=:sid"""),
                       {"e": 1 if ev.payload.get("eligible") else 0, "tier": ev.payload.get("tier"), "sid": ev.session_id})
        if ev.event_type == "loads_pitched":
            pl = ev.payload
            cx.execute(text("""UPDATE sessions SET load_id=:lid, listed_rate=:rate, miles=:mi,
                               origin=:o, destination=:d, equipment_type=:eq WHERE session_id=:sid"""),
                       {"lid": pl.get("load_id"), "rate": pl.get("loadboard_rate"),
                        "mi": pl.get("miles"), "o": pl.get("origin"), "d": pl.get("destination"),
                        "eq": pl.get("equipment_type"), "sid": ev.session_id})
        if ev.event_type == "negotiation_round":
            cx.execute(text("""UPDATE sessions SET rounds=COALESCE(rounds,0)+1,
                               final_rate=:fr WHERE session_id=:sid"""),
                       {"fr": ev.payload.get("next_offer") or ev.payload.get("carrier_offer"), "sid": ev.session_id})
        if ev.event_type == "outcome":
            cx.execute(text("""UPDATE sessions SET outcome=:o, final_rate=:r WHERE session_id=:sid"""),
                       {"o": ev.payload.get("outcome"), "r": ev.payload.get("final_rate"), "sid": ev.session_id})
        if ev.event_type == "sentiment":
            cx.execute(text("""UPDATE sessions SET sentiment=:s WHERE session_id=:sid"""),
                       {"s": ev.payload.get("label"), "sid": ev.session_id})
    return {"ok": True}

@router.get("/dashboard/summary")
def dashboard_summary():
    with engine.begin() as cx:
        totals = cx.execute(text("SELECT COUNT(*) FROM sessions")).scalar() or 0
        acc   = cx.execute(text("SELECT COUNT(*) FROM sessions WHERE outcome='accept'")).scalar() or 0
        rounds= cx.execute(text("SELECT ROUND(AVG(COALESCE(rounds,0)),2) FROM sessions")).scalar() or 0
        rpm   = cx.execute(text("""
            SELECT ROUND(AVG(CASE WHEN miles>0 THEN CAST(final_rate AS FLOAT)/miles END),2) FROM sessions
        """)).scalar()
        top_lanes = cx.execute(text("""
            SELECT origin||' → '||destination AS lane, COUNT(*) c
            FROM sessions GROUP BY lane ORDER BY c DESC LIMIT 5
        """)).mappings().all()
    return {
        "total_calls": totals,
        "accept_rate": (acc / totals) if totals else 0.0,
        "avg_rounds": rounds,
        "avg_rate_per_mile": rpm or 0,
        "top_lanes": list(top_lanes),
    }