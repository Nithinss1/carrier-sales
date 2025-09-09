# app/telemetry.py
from fastapi import APIRouter, Body
import time, uuid
from typing import Any, Dict, List

# In-memory store (swap for DB later)
SESSIONS: Dict[str, Dict[str, Any]] = {}
EVENTS: Dict[str, List[Dict[str, Any]]] = {}

# ---- Local helpers you can call from your code ----
def start_session(caller=None, session_id: str | None=None) -> str:
    sid = session_id or str(uuid.uuid4())
    SESSIONS[sid] = {"session_id": sid, "started_at": time.time(), "caller": caller}
    EVENTS.setdefault(sid, [])
    return sid

def end_session(session_id: str, summary: Dict[str, Any] | None=None):
    SESSIONS.setdefault(session_id, {})["ended_at"] = time.time()
    if summary: SESSIONS[session_id]["summary"] = summary

def log_event(session_id: str, event_type: str, data: Dict[str, Any]):
    EVENTS.setdefault(session_id, []).append({"ts": time.time(), "type": event_type, "data": data})

def log_verify_result(sid, mc, status, eligible, tier, risk_score):
    log_event(sid, "verify_result", {"mc": mc, "status": status, "eligible": eligible, "carrier_tier": tier, "risk_score": risk_score})

def log_loads_pitched(sid, loads): log_event(sid, "loads_pitched", {"loads": loads})
def log_negotiation_round(sid, round_num, load_id, listed_rate, our_offer, carrier_offer, decision, next_offer, cap_rate):
    log_event(sid, "negotiation_round", {
        "round": round_num, "load_id": load_id, "listed_rate": listed_rate,
        "our_offer": our_offer, "carrier_offer": carrier_offer,
        "decision": decision, "next_offer": next_offer, "cap_rate": cap_rate
    })
def log_outcome(sid, outcome, final_rate=None, load_id=None): log_event(sid, "outcome", {"outcome": outcome, "final_rate": final_rate, "load_id": load_id})
def log_sentiment(sid, label, score=None): log_event(sid, "sentiment", {"label": label, "score": score})

# ---- Optional API for external dashboards ----
router = APIRouter(prefix="/log", tags=["telemetry"])

@router.post("/session/start")
def session_start_api(body: Dict[str, Any] = Body(...)):
    sid = start_session(caller=body.get("caller"), session_id=body.get("session_id"))
    return {"session_id": sid}

@router.post("/session/end")
def session_end_api(body: Dict[str, Any] = Body(...)):
    end_session(body["session_id"], summary=body.get("summary"))
    return {"ok": True}

@router.post("/events")
def events_api(body: Dict[str, Any] = Body(...)):
    log_event(body["session_id"], body.get("event_type","event"), body.get("data", {}))
    return {"ok": True}

@router.get("/events/{session_id}")
def get_events(session_id: str):
    return {"session": SESSIONS.get(session_id), "events": EVENTS.get(session_id, [])}
