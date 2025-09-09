# app/telemetry.py
from fastapi import APIRouter, Body
import time, uuid
from typing import Any, Dict, List
from statistics import mean


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


def _events_of(sid, t): 
    return [e for e in EVENTS.get(sid, []) if e["type"] == t]
def _latest_of(sid, t):
    evs = _events_of(sid, t)
    return evs[-1] if evs else None

@router.get("/summary")
def log_summary():
    total_sessions = len(SESSIONS)
    outcome_counts = {"accept":0, "decline":0, "callback":0, "counter":0, "info_only":0}
    sentiment_counts = {"positive":0, "neutral":0, "negative":0}
    rounds_per_session = []
    deltas_abs = []   # final_rate - listed_rate
    deltas_pct = []   # above/below list in %

    for sid in SESSIONS.keys():
        # rounds
        rounds = len(_events_of(sid, "negotiation_round"))
        rounds_per_session.append(rounds)

        # outcome
        o = _latest_of(sid, "outcome")
        if o:
            outcome = (o["data"].get("outcome") or "").lower()
            if outcome in outcome_counts:
                outcome_counts[outcome] += 1

        # sentiment
        s = _latest_of(sid, "sentiment")
        if s:
            label = (s["data"].get("label") or "").lower()
            if label in sentiment_counts:
                sentiment_counts[label] += 1

        # rate deltas (if we have both)
        lp = _latest_of(sid, "loads_pitched")
        fr = o and o["data"].get("final_rate")
        if lp and fr is not None:
            try:
                listed = int(lp["data"]["loads"][0]["loadboard_rate"])
                final  = int(fr)
                deltas_abs.append(final - listed)
                if listed:
                    deltas_pct.append((final - listed)/listed)
            except Exception:
                pass

    accept = outcome_counts["accept"]
    acc_rate = (accept / total_sessions) if total_sessions else 0.0

    return {
        "totals": {
            "sessions": total_sessions,
            "accept_rate": round(acc_rate, 3),
            "avg_rounds": round(mean(rounds_per_session), 2) if rounds_per_session else 0
        },
        "mix": {
            "outcomes": outcome_counts,
            "sentiment": sentiment_counts
        },
        "pricing": {
            "avg_delta_abs": round(mean(deltas_abs), 2) if deltas_abs else 0,
            "avg_delta_pct": round(mean(deltas_pct), 3) if deltas_pct else 0
        }
    }

@router.get("/recent")
def log_recent(limit: int = 10):
    # last N sessions with key facts
    rows = []
    for sid, sess in list(SESSIONS.items())[-limit:]:
        lp = _latest_of(sid, "loads_pitched")
        o  = _latest_of(sid, "outcome")
        s  = _latest_of(sid, "sentiment")
        rows.append({
            "session_id": sid,
            "started_at": sess.get("started_at"),
            "outcome": (o and o["data"].get("outcome")) or None,
            "final_rate": (o and o["data"].get("final_rate")) or None,
            "listed_rate": (lp and lp["data"]["loads"][0].get("loadboard_rate")) if lp else None,
            "lane": (lp and f'{lp["data"]["loads"][0].get("origin")} → {lp["data"]["loads"][0].get("destination")}') if lp else None,
            "sentiment": (s and s["data"].get("label")) or None,
            "rounds": len(_events_of(sid, "negotiation_round")),
        })
    return {"items": rows[::-1]}