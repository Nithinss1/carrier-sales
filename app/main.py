
import os, math
import json
import sqlite3
from datetime import datetime
from typing import Optional, Dict, Any, Literal
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import httpx

from .telemetry import (
    start_session, log_verify_result, log_loads_pitched,
    log_negotiation_round, log_outcome, log_sentiment
)


API_KEY = os.getenv("API_KEY", "supersecret123")
FMCSA_API_KEY = os.getenv("FMCSA_API_KEY")
CARRIER_UPSTREAM_URL = os.getenv("CARRIER_UPSTREAM_URL", "").strip()
CARRIER_UPSTREAM_HEADER = os.getenv("CARRIER_UPSTREAM_HEADER", "API_KEY")
CARRIER_UPSTREAM_KEY = os.getenv("CARRIER_UPSTREAM_KEY", "").strip()
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "../data.db"))
from .telemetry import router as telemetry_router
app = FastAPI(title="Inbound Carrier Sales API", version="0.1.0")
app.include_router(telemetry_router)


def _require(x_api_key: str | None):
    if x_api_key != API_KEY:
        raise HTTPException(401, "Unauthorized")

class VerifyPayload(BaseModel):
    mc: str
    caller_number: Optional[str] = None
    session_id: Optional[str] = None

class EvaluateIn(BaseModel):
    load_id: str
    listed_rate: int          # board rate (your starting anchor)
    our_offer: int            # what you last quoted to the carrier
    carrier_offer: int        # what the carrier is asking now
    miles: Optional[int] = 0
    equipment_type: Optional[str] = "Dry Van"
    round: int                # 1..3
    session_id: Optional[str] = None

class EvaluateOut(BaseModel):
    decision: Literal["accept", "counter"]
    next_offer: int           # accept at this price, or your new counter
    cap_rate: int             # max you’ll pay this call
    floor_rate: int           # just informational (not used here)
    round_next: int           # pass this back on the next call
    reason: str

class SearchPayload(BaseModel):
    origin: Optional[str] = None
    destination: Optional[str] = None
    pickup_start: Optional[str] = None
    pickup_end: Optional[str] = None
    equipment_type: Optional[str] = None
    max_results: int = 3
    session_id: Optional[str] = None

class EvaluatePayload(BaseModel):
    load_id: str
    listed_rate: float
    carrier_offer: float
    tier: Optional[str] = "standard"
    miles: Optional[float] = None
    equipment_type: Optional[str] = None
    round: int = 1

class LogPayload(BaseModel):
    call_id: str
    mc: Optional[str] = None
    load_id: Optional[str] = None
    listed_rate: Optional[float] = None
    final_rate: Optional[float] = None
    rounds: Optional[int] = 0
    outcome: Optional[str] = None
    sentiment: Optional[str] = None
    extra: Optional[Dict[str, Any]] = None
    timestamp: Optional[str] = None

def auth(x_api_key: Optional[str]):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS calls(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            call_id TEXT,
            mc TEXT,
            load_id TEXT,
            listed_rate REAL,
            final_rate REAL,
            rounds INTEGER,
            outcome TEXT,
            sentiment TEXT,
            extra TEXT,
            ts TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

with open(os.path.join(os.path.dirname(__file__), "loads.json"), "r") as f:
    LOADS = json.load(f)

def normalize(s: Optional[str]) -> str:
    return (s or "").strip().lower()

def parse_iso(ts: Optional[str]):
    if not ts: return None
    try:
        return datetime.fromisoformat(ts.replace("Z",""))
    except:
        return None
    
def _round25(x: float) -> int:
    return int(round(x / 25.0) * 25)

@app.get("/healthz")
def healthz():
    return {"ok": True, "version": "evaluate-v2"}

@app.post("/debug_echo")
def debug_echo(p: dict, x_api_key: str = Header(None)):
    _require(x_api_key)
    return {"received": p}

@app.post("/verify_carrier")
async def verify_carrier(payload: VerifyPayload, x_api_key: Optional[str] = Header(None), x_session_id: Optional[str] = Header(None)):
    auth(x_api_key)
    sid = payload.session_id or x_session_id or start_session(caller="inbound_voice")
    mc = "".join([c for c in payload.mc if c.isdigit()])
    if not mc:
        raise HTTPException(400, "Missing MC")
    if CARRIER_UPSTREAM_URL:
        headers = {CARRIER_UPSTREAM_HEADER: CARRIER_UPSTREAM_KEY} if CARRIER_UPSTREAM_KEY else {}
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                r = await client.post(CARRIER_UPSTREAM_URL, params={"mc": mc}, headers=headers)
                r.raise_for_status()
                u = r.json()
                result = {
                    "mc": mc,
                    "dot": u.get("dot"),
                    "eligible": bool(u.get("eligible", False)),
                    "status": u.get("status", "unknown"),
                    "risk_score": u.get("risk_score", 50),
                    "carrier_tier": u.get("carrier_tier", "standard"),
                    "business_recommendation": u.get("business_recommendation", "manual_review_required"),
                    "verification_timestamp": datetime.utcnow().isoformat() + "Z"
                }
                log_verify_result(
                    sid, mc, result.get("status"), result.get("eligible"),
                    result.get("carrier_tier"), result.get("risk_score")
                )
                result["session_id"] = sid
                return result
            except Exception:
                pass
    ineligible = {"000111", "999999", "123"}
    eligible = mc not in ineligible
    result = {
        "mc": mc,
        "dot": None,
        "eligible": eligible,
        "status": "authorized" if eligible else "not_authorized",
        "risk_score": 30 if eligible else 80,
        "carrier_tier": "silver" if eligible else "bronze",
        "business_recommendation": "ok_to_proceed" if eligible else "manual_review_required",
        "verification_timestamp": datetime.utcnow().isoformat() + "Z"
    }
    log_verify_result(
        sid, mc, result.get("status"), result.get("eligible"),
        result.get("carrier_tier"), result.get("risk_score")
    )
    result["session_id"] = sid
    return result

@app.post("/search_loads")
def search_loads(payload: SearchPayload, x_api_key: Optional[str] = Header(None), x_session_id: Optional[str] = Header(None)):
    auth(x_api_key)
    sid = payload.session_id or x_session_id or start_session()
    o = normalize(payload.origin)
    d = normalize(payload.destination)
    et = normalize(payload.equipment_type)
    ps = parse_iso(payload.pickup_start)
    pe = parse_iso(payload.pickup_end)

    results = []
    for L in LOADS:
        score = 0
        if o and normalize(L.get("origin")).startswith(o[:5]): score += 2
        if d and normalize(L.get("destination")).startswith(d[:5]): score += 2
        if et and normalize(L.get("equipment_type")) == et: score += 1
        pdt = parse_iso(L.get("pickup_datetime"))
        if ps and pe and pdt and (ps <= pdt <= pe): score += 1
        if score > 0: results.append((score, L))
    results.sort(key=lambda x: x[0], reverse=True)
    loads = [r[1] for r in results[: max(1, payload.max_results)]]
    log_loads_pitched(sid, loads)
    return {"session_id": sid, "loads": loads}

@app.post("/evaluate_offer")
def evaluate_offer(p: EvaluateIn, x_api_key: str = Header(None), x_session_id: Optional[str] = Header(None)):
    _require(x_api_key)
    sid = p.session_id or x_session_id or start_session()
    try:
        # --- guardrails ---
        MAX_OVER_LISTED_PCT = 0.15
        MIN_STEP            = 50
        CLOSE_GAP           = 50
        SHORT_HAUL_BUMP     = 100 if (p.miles or 0) < 300 else 0
        EQUIP_BUMP_MAP      = {"Reefer": 75, "Flatbed": 100}
        equip_bump          = EQUIP_BUMP_MAP.get(p.equipment_type or "", 0)
        round_bump          = max(0, p.round - 1) * 50

        base_cap = int(round(p.listed_rate * (1 + MAX_OVER_LISTED_PCT)))
        cap_rate = base_cap + SHORT_HAUL_BUMP + equip_bump + round_bump

        # 1) If the carrier is <= your current offer → accept (you pay less).
        if p.carrier_offer <= p.our_offer:
            resp = {
                "decision": "accept",
                "next_offer": int(p.carrier_offer),
                "round_next": p.round + 1,
                "cap_rate": int(cap_rate),
                "reason": "carrier at/below current offer"
            }
            log_negotiation_round(
                sid, p.round, p.load_id, p.listed_rate,
                p.our_offer, p.carrier_offer,
                resp.get("decision"), resp.get("next_offer"), resp.get("cap_rate")
            )
            resp["session_id"] = sid
            return resp

        # 2) Carrier above your offer. If within cap and late/close → accept.
        if p.carrier_offer <= cap_rate and (p.round >= 3 or (p.carrier_offer - p.our_offer) <= CLOSE_GAP):
            resp = {
                "decision": "accept",
                "next_offer": int(p.carrier_offer),
                "round_next": p.round + 1,
                "cap_rate": int(cap_rate),
                "reason": "within cap and close/late round"
            }
            log_negotiation_round(
                sid, p.round, p.load_id, p.listed_rate,
                p.our_offer, p.carrier_offer,
                resp.get("decision"), resp.get("next_offer"), resp.get("cap_rate")
            )
            resp["session_id"] = sid
            return resp

        # 3) Otherwise counter upward toward them, but never above cap.
        gap        = p.carrier_offer - p.our_offer
        step       = max(MIN_STEP, int(math.ceil(0.5 * gap)))  # ~split the diff
        next_offer = min(cap_rate, p.our_offer + step)

        resp = {
            "decision": "counter",
            "next_offer": int(next_offer),
            "round_next": p.round + 1,
            "cap_rate": int(cap_rate),
            "reason": "counter under cap"
        }
        log_negotiation_round(
            sid, p.round, p.load_id, p.listed_rate,
            p.our_offer, p.carrier_offer,
            resp.get("decision"), resp.get("next_offer"), resp.get("cap_rate")
        )
        resp["session_id"] = sid
        return resp

    except Exception as e:
        # Return error details so you don't have to dig logs during setup
        return {"error": "negotiation_error", "detail": str(e), "trace": traceback.format_exc()}

@app.post("/classify_and_log")
def classify_and_log(p: LogPayload, x_api_key: Optional[str] = Header(None)):
    auth(x_api_key)
    ts = p.timestamp or datetime.utcnow().isoformat() + "Z"
    conn = db_conn()
    conn.execute("INSERT INTO calls(call_id, mc, load_id, listed_rate, final_rate, rounds, outcome, sentiment, extra, ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (p.call_id, p.mc, p.load_id, p.listed_rate, p.final_rate, p.rounds, p.outcome, p.sentiment, json.dumps(p.extra or {}), ts))
    conn.commit(); conn.close()
    return {"ok": True, "ts": ts}

@app.get("/metrics")
def metrics():
    conn = db_conn()
    rows = conn.execute("SELECT outcome, sentiment, rounds, listed_rate, final_rate FROM calls").fetchall()
    conn.close()
    total = len(rows); by_outcome = {}; sentiments = {}; rounds_sum = 0; delta_sum = 0.0
    for r in rows:
        by_outcome[r["outcome"]] = by_outcome.get(r["outcome"],0) + 1
        sentiments[r["sentiment"]] = sentiments.get(r["sentiment"],0) + 1
        rounds_sum += (r["rounds"] or 0)
        if r["listed_rate"] and r["final_rate"]:
            delta_sum += float(r["final_rate"]) - float(r["listed_rate"])
    return {
        "total_calls": total,
        "by_outcome": by_outcome,
        "sentiments": sentiments,
        "rounds_avg": round(rounds_sum/total,2) if total else 0,
        "delta_avg": round(delta_sum/total,2) if total else 0
    }

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return """
    <html><body>
      <h2>Inbound Carrier Sales – Dashboard</h2>
      <pre id="sum">Loading summary…</pre>
      <pre id="rec">Loading recent…</pre>
      <script>
       async function go(){
         const s = await (await fetch('/log/summary')).json();
         const r = await (await fetch('/log/recent?limit=10')).json();
         document.getElementById('sum').textContent = JSON.stringify(s, null, 2);
         document.getElementById('rec').textContent = JSON.stringify(r, null, 2);
       }
       go();
      </script>
    </body></html>
    """
