
import os
import json
import sqlite3
from datetime import datetime
from typing import Optional, Dict, Any
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import httpx

API_KEY = os.getenv("SERVICE_API_KEY", "supersecret")
FMCSA_API_KEY = os.getenv("FMCSA_API_KEY")
CARRIER_UPSTREAM_URL = os.getenv("CARRIER_UPSTREAM_URL", "").strip()
CARRIER_UPSTREAM_HEADER = os.getenv("CARRIER_UPSTREAM_HEADER", "x-api-key")
CARRIER_UPSTREAM_KEY = os.getenv("CARRIER_UPSTREAM_KEY", "").strip()

DB_PATH = os.getenv("DB_PATH", "data.db")

app = FastAPI(title="Inbound Carrier Sales API", version="0.1.0")

class VerifyPayload(BaseModel):
    mc: str
    caller_number: Optional[str] = None

class SearchPayload(BaseModel):
    origin: Optional[str] = None
    destination: Optional[str] = None
    pickup_start: Optional[str] = None
    pickup_end: Optional[str] = None
    equipment_type: Optional[str] = None
    max_results: int = 3

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

@app.get("/healthz")
def health():
    return {"ok": True, "time": datetime.utcnow().isoformat() + "Z"}

@app.post("/verify_carrier")
async def verify_carrier(payload: VerifyPayload, x_api_key: Optional[str] = Header(None)):
    auth(x_api_key)
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
                return {
                    "mc": mc,
                    "dot": u.get("dot"),
                    "eligible": bool(u.get("eligible", False)),
                    "status": u.get("status", "unknown"),
                    "risk_score": u.get("risk_score", 50),
                    "carrier_tier": u.get("carrier_tier", "standard"),
                    "business_recommendation": u.get("business_recommendation", "manual_review_required"),
                    "verification_timestamp": datetime.utcnow().isoformat() + "Z"
                }
            except Exception:
                pass
    ineligible = {"000111", "999999", "123"}
    eligible = mc not in ineligible
    return {
        "mc": mc,
        "dot": None,
        "eligible": eligible,
        "status": "authorized" if eligible else "not_authorized",
        "risk_score": 30 if eligible else 80,
        "carrier_tier": "silver" if eligible else "bronze",
        "business_recommendation": "ok_to_proceed" if eligible else "manual_review_required",
        "verification_timestamp": datetime.utcnow().isoformat() + "Z"
    }

@app.post("/search_loads")
def search_loads(payload: SearchPayload, x_api_key: Optional[str] = Header(None)):
    auth(x_api_key)
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
    return {"loads": loads}

@app.post("/evaluate_offer")
def evaluate_offer(p: EvaluatePayload, x_api_key: Optional[str] = Header(None)):
    auth(x_api_key)
    listed = float(p.listed_rate); offer = float(p.carrier_offer); rnd = int(p.round)
    tier_floor_factor = {"gold": 0.93, "silver": 0.9, "standard": 0.88, "bronze": 0.87}.get((p.tier or "standard").lower(), 0.88)
    abs_floor = listed * tier_floor_factor
    if rnd >= 3 and offer >= abs_floor:
        return {"decision": "accept", "next_offer": offer, "rationale": "final round within floor"}
    if offer >= abs_floor and offer >= listed * 0.9:
        return {"decision": "accept", "next_offer": offer, "rationale": "meets policy floor"}
    counter = min(listed - 50, (offer + listed) / 2)
    if counter < abs_floor: counter = abs_floor
    return {"decision":"counter","next_offer":round(counter,2),"rationale":f"split diff; floor={round(abs_floor,2)}"}

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
    return HTMLResponse("""
    <!doctype html><html><head><meta charset='utf-8'>
    <title>Inbound Carrier Sales Dashboard</title>
    <meta name='viewport' content='width=device-width, initial-scale=1'>
    <script src='https://cdn.jsdelivr.net/npm/chart.js'></script>
    <style>body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;margin:24px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:24px}.card{padding:16px;border:1px solid #eee;border-radius:12px;box-shadow:0 2px 8px rgba(0,0,0,0.05)}h1{margin:0 0 12px 0}</style>
    </head><body><h1>Inbound Carrier Sales — KPIs</h1>
    <div class='grid'><div class='card'><canvas id='outcome'></canvas></div>
    <div class='card'><canvas id='sentiment'></canvas></div>
    <div class='card'><canvas id='rounds'></canvas></div>
    <div class='card'><canvas id='delta'></canvas></div></div>
    <script>
    async function load(){const r=await fetch('/metrics');const m=await r.json();
      const outcomes=Object.keys(m.by_outcome||{});const oc=outcomes.map(k=>m.by_outcome[k]);
      const sentiments=Object.keys(m.sentiments||{});const sc=sentiments.map(k=>m.sentiments[k]);
      new Chart(document.getElementById('outcome'),{type:'bar',data:{labels:outcomes,datasets:[{label:'Calls',data:oc}]}})
      new Chart(document.getElementById('sentiment'),{type:'bar',data:{labels:sentiments,datasets:[{label:'Count',data:sc}]}})
      new Chart(document.getElementById('rounds'),{type:'bar',data:{labels:['Avg Rounds'],datasets:[{label:'Rounds',data:[m.rounds_avg]}]}})
      new Chart(document.getElementById('delta'),{type:'bar',data:{labels:['Avg $ Delta (final - listed)'],datasets:[{label:'Delta',data:[m.delta_avg]}]}})
    } load();
    </script></body></html>
    """ )
