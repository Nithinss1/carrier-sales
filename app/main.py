
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
    router as telemetry_router,
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

def round_to_25(x: float) -> int:
    # always round to the nearest 25
    return int(round(x / 25.0) * 25)

def compute_cap(listed_rate: int, miles: int | None, equipment_type: str | None) -> int:
    # Stable cap: depends only on load facts (not round or carrier ask)
    base = min(325, int(0.25 * listed_rate))
    equip_add = 75 if (equipment_type or "").lower() in ("reefer", "flatbed") else 0
    shorthaul_add = 50 if (miles is not None and miles < 300) else 0
    cap = listed_rate + base + equip_add + shorthaul_add
    return round_to_25(cap)

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

    listed = int(p.listed_rate)
    prev   = int(p.our_offer)           # ← IMPORTANT: pass last next_offer here!
    ask    = int(p.carrier_offer)
    miles  = p.miles
    equip  = p.equipment_type or ""
    rnd    = int(p.round)

    cap = compute_cap(listed, miles, equip)  # ← STABLE per load/session

    # If carrier is already at/below our current price, or very close late in the game, accept.
    if ask <= cap and (ask <= prev or (rnd >= 3 and (ask - prev) <= 50)):
        resp = {
            "decision": "accept",
            "next_offer": ask,
            "round_next": rnd + 1,
            "cap_rate": cap,
            "reason": "carrier within cap and close enough",
            "session_id": sid
        }
        log_negotiation_round(
            sid, rnd, p.load_id, listed,
            prev, ask,
            resp.get("decision"), resp.get("next_offer"), resp.get("cap_rate")
        )
        return resp

    # Otherwise counter toward the smaller of (carrier ask, cap)
    target = min(cap, ask)
    gap = target - prev
    if gap <= 0:
        # We’re already at/above target; hold line
        next_offer = prev
    else:
        # Concession schedule by round (monotonic ↑)
        ratio = {1: 0.35, 2: 0.25, 3: 0.20}.get(rnd, 0.15)
        increment = max(25, round_to_25(gap * ratio))
        next_offer = min(cap, prev + increment)

    # MONOTONIC GUARANTEE
    if next_offer < prev:
        next_offer = prev

    # On final round, go to cap if still below target
    if rnd >= 3 and next_offer < target:
        next_offer = min(cap, max(prev, next_offer))

    resp = {
        "decision": "counter",
        "next_offer": next_offer,
        "round_next": rnd + 1,
        "cap_rate": cap,
        "reason": "counter toward target within cap",
        "session_id": sid
    }
    log_negotiation_round(
        sid, rnd, p.load_id, listed,
        prev, ask,
        resp.get("decision"), resp.get("next_offer"), resp.get("cap_rate")
    )
    return resp

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
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Inbound Carrier Sales Dashboard</title>
        <style>
            * {
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }
            
            body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                padding: 20px;
            }
            
            .container {
                max-width: 1400px;
                margin: 0 auto;
            }
            
            .header {
                text-align: center;
                margin-bottom: 40px;
                color: white;
            }
            
            .header h1 {
                font-size: 2.5rem;
                font-weight: 700;
                margin-bottom: 8px;
                text-shadow: 0 2px 4px rgba(0,0,0,0.3);
            }
            
            .header p {
                font-size: 1.1rem;
                opacity: 0.9;
            }
            
            .dashboard-grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(350px, 1fr));
                gap: 24px;
                margin-bottom: 40px;
            }
            
            .card {
                background: rgba(255, 255, 255, 0.95);
                backdrop-filter: blur(10px);
                border-radius: 16px;
                padding: 24px;
                box-shadow: 0 8px 32px rgba(0,0,0,0.1);
                border: 1px solid rgba(255,255,255,0.2);
                transition: transform 0.2s ease, box-shadow 0.2s ease;
            }
            
            .card:hover {
                transform: translateY(-2px);
                box-shadow: 0 12px 40px rgba(0,0,0,0.15);
            }
            
            .card-header {
                display: flex;
                align-items: center;
                margin-bottom: 20px;
            }
            
            .card-icon {
                width: 48px;
                height: 48px;
                border-radius: 12px;
                display: flex;
                align-items: center;
                justify-content: center;
                margin-right: 16px;
                font-size: 24px;
            }
            
            .stats-icon {
                background: linear-gradient(45deg, #ff6b6b, #ee5a6f);
            }
            
            .outcomes-icon {
                background: linear-gradient(45deg, #4ecdc4, #44a08d);
            }
            
            .sentiment-icon {
                background: linear-gradient(45deg, #45b7d1, #96c93d);
            }
            
            .pricing-icon {
                background: linear-gradient(45deg, #f093fb, #f5576c);
            }
            
            .card-title {
                font-size: 1.3rem;
                font-weight: 600;
                color: #2d3748;
            }
            
            .metric-row {
                display: flex;
                justify-content: space-between;
                align-items: center;
                padding: 12px 0;
                border-bottom: 1px solid #e2e8f0;
            }
            
            .metric-row:last-child {
                border-bottom: none;
            }
            
            .metric-label {
                font-weight: 500;
                color: #4a5568;
            }
            
            .metric-value {
                font-weight: 600;
                font-size: 1.1rem;
                color: #2d3748;
            }
            
            .metric-value.success {
                color: #38a169;
            }
            
            .metric-value.warning {
                color: #d69e2e;
            }
            
            .metric-value.danger {
                color: #e53e3e;
            }
            
            .recent-section {
                background: rgba(255, 255, 255, 0.95);
                backdrop-filter: blur(10px);
                border-radius: 16px;
                padding: 24px;
                box-shadow: 0 8px 32px rgba(0,0,0,0.1);
                border: 1px solid rgba(255,255,255,0.2);
            }
            
            .section-title {
                font-size: 1.4rem;
                font-weight: 600;
                color: #2d3748;
                margin-bottom: 20px;
                display: flex;
                align-items: center;
            }
            
            .section-title::before {
                content: "📋";
                margin-right: 12px;
                font-size: 1.2rem;
            }
            
            .table {
                width: 100%;
                border-collapse: collapse;
                background: white;
                border-radius: 8px;
                overflow: hidden;
                box-shadow: 0 2px 8px rgba(0,0,0,0.05);
            }
            
            .table th {
                background: #f7fafc;
                padding: 12px 16px;
                text-align: left;
                font-weight: 600;
                color: #4a5568;
                border-bottom: 2px solid #e2e8f0;
            }
            
            .table td {
                padding: 12px 16px;
                border-bottom: 1px solid #e2e8f0;
                color: #2d3748;
            }
            
            .table tr:hover {
                background: #f7fafc;
            }
            
            .status-badge {
                padding: 4px 12px;
                border-radius: 20px;
                font-size: 0.85rem;
                font-weight: 500;
                text-transform: capitalize;
            }
            
            .status-accept {
                background: #c6f6d5;
                color: #22543d;
            }
            
            .status-decline {
                background: #fed7d7;
                color: #742a2a;
            }
            
            .status-callback {
                background: #feebc8;
                color: #7c2d12;
            }
            
            .status-pending {
                background: #e2e8f0;
                color: #4a5568;
            }
            
            .sentiment-positive {
                color: #38a169;
                font-weight: 600;
            }
            
            .sentiment-negative {
                color: #e53e3e;
                font-weight: 600;
            }
            
            .sentiment-neutral {
                color: #718096;
                font-weight: 600;
            }
            
            .loading {
                text-align: center;
                padding: 40px;
                color: #718096;
                font-style: italic;
            }
            
            .refresh-btn {
                position: fixed;
                bottom: 24px;
                right: 24px;
                background: linear-gradient(45deg, #667eea, #764ba2);
                color: white;
                border: none;
                width: 56px;
                height: 56px;
                border-radius: 50%;
                font-size: 24px;
                cursor: pointer;
                box-shadow: 0 4px 16px rgba(0,0,0,0.2);
                transition: transform 0.2s ease;
            }
            
            .refresh-btn:hover {
                transform: scale(1.1);
            }
            
            @media (max-width: 768px) {
                .dashboard-grid {
                    grid-template-columns: 1fr;
                }
                
                .header h1 {
                    font-size: 2rem;
                }
                
                .table {
                    font-size: 0.9rem;
                }
                
                .table th,
                .table td {
                    padding: 8px 12px;
                }
            }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>🚛 Inbound Carrier Sales</h1>
                <p>Real-time performance dashboard</p>
            </div>
            
            <div class="dashboard-grid">
                <div class="card">
                    <div class="card-header">
                        <div class="card-icon stats-icon">📊</div>
                        <div class="card-title">Key Statistics</div>
                    </div>
                    <div id="stats-content" class="loading">Loading statistics...</div>
                </div>
                
                <div class="card">
                    <div class="card-header">
                        <div class="card-icon outcomes-icon">🎯</div>
                        <div class="card-title">Call Outcomes</div>
                    </div>
                    <div id="outcomes-content" class="loading">Loading outcomes...</div>
                </div>
                
                <div class="card">
                    <div class="card-header">
                        <div class="card-icon sentiment-icon">😊</div>
                        <div class="card-title">Sentiment Analysis</div>
                    </div>
                    <div id="sentiment-content" class="loading">Loading sentiment...</div>
                </div>
                
                <div class="card">
                    <div class="card-header">
                        <div class="card-icon pricing-icon">💰</div>
                        <div class="card-title">Pricing Metrics</div>
                    </div>
                    <div id="pricing-content" class="loading">Loading pricing...</div>
                </div>
            </div>
            
            <div class="recent-section">
                <div class="section-title">Recent Sessions</div>
                <div id="recent-content" class="loading">Loading recent sessions...</div>
            </div>
        </div>
        
        <button class="refresh-btn" onclick="loadDashboard()" title="Refresh Dashboard">🔄</button>
        
        <script>
            function formatCurrency(value) {
                return new Intl.NumberFormat('en-US', {
                    style: 'currency',
                    currency: 'USD',
                    minimumFractionDigits: 0
                }).format(value);
            }
            
            function formatPercentage(value) {
                return (value * 100).toFixed(1) + '%';
            }
            
            function formatTimestamp(timestamp) {
                if (!timestamp) return 'N/A';
                return new Date(timestamp * 1000).toLocaleString();
            }
            
            function getStatusBadge(outcome) {
                if (!outcome) return '<span class="status-badge status-pending">Pending</span>';
                const className = `status-${outcome.toLowerCase()}`;
                return `<span class="status-badge ${className}">${outcome}</span>`;
            }
            
            function getSentimentClass(sentiment) {
                if (!sentiment) return '';
                return `sentiment-${sentiment.toLowerCase()}`;
            }
            
            async function loadDashboard() {
                try {
                    // Load summary data
                    const summaryResponse = await fetch('/log/summary');
                    const summary = await summaryResponse.json();
                    
                    // Update stats
                    document.getElementById('stats-content').innerHTML = `
                        <div class="metric-row">
                            <span class="metric-label">Total Sessions</span>
                            <span class="metric-value">${summary.totals.sessions}</span>
                        </div>
                        <div class="metric-row">
                            <span class="metric-label">Accept Rate</span>
                            <span class="metric-value success">${formatPercentage(summary.totals.accept_rate)}</span>
                        </div>
                        <div class="metric-row">
                            <span class="metric-label">Avg Rounds</span>
                            <span class="metric-value">${summary.totals.avg_rounds}</span>
                        </div>
                    `;
                    
                    // Update outcomes
                    const outcomes = summary.mix.outcomes;
                    document.getElementById('outcomes-content').innerHTML = `
                        <div class="metric-row">
                            <span class="metric-label">Accepted</span>
                            <span class="metric-value success">${outcomes.accept || 0}</span>
                        </div>
                        <div class="metric-row">
                            <span class="metric-label">Declined</span>
                            <span class="metric-value danger">${outcomes.decline || 0}</span>
                        </div>
                        <div class="metric-row">
                            <span class="metric-label">Callbacks</span>
                            <span class="metric-value warning">${outcomes.callback || 0}</span>
                        </div>
                    `;
                    
                    // Update sentiment
                    const sentiment = summary.mix.sentiment;
                    document.getElementById('sentiment-content').innerHTML = `
                        <div class="metric-row">
                            <span class="metric-label">Positive</span>
                            <span class="metric-value success">${sentiment.positive || 0}</span>
                        </div>
                        <div class="metric-row">
                            <span class="metric-label">Neutral</span>
                            <span class="metric-value">${sentiment.neutral || 0}</span>
                        </div>
                        <div class="metric-row">
                            <span class="metric-label">Negative</span>
                            <span class="metric-value danger">${sentiment.negative || 0}</span>
                        </div>
                    `;
                    
                    // Update pricing
                    document.getElementById('pricing-content').innerHTML = `
                        <div class="metric-row">
                            <span class="metric-label">Avg Delta</span>
                            <span class="metric-value">${formatCurrency(summary.pricing.avg_delta_abs)}</span>
                        </div>
                        <div class="metric-row">
                            <span class="metric-label">Avg Delta %</span>
                            <span class="metric-value">${formatPercentage(summary.pricing.avg_delta_pct)}</span>
                        </div>
                    `;
                    
                    // Load recent sessions
                    const recentResponse = await fetch('/log/recent?limit=10');
                    const recent = await recentResponse.json();
                    
                    if (recent.items && recent.items.length > 0) {
                        const tableHTML = `
                            <table class="table">
                                <thead>
                                    <tr>
                                        <th>Session</th>
                                        <th>Lane</th>
                                        <th>Listed Rate</th>
                                        <th>Final Rate</th>
                                        <th>Outcome</th>
                                        <th>Sentiment</th>
                                        <th>Started</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    ${recent.items.map(item => `
                                        <tr>
                                            <td style="font-family: monospace; font-size: 0.9rem;">${item.session_id.substring(0, 8)}...</td>
                                            <td>${item.lane || 'N/A'}</td>
                                            <td>${item.listed_rate ? formatCurrency(item.listed_rate) : 'N/A'}</td>
                                            <td>${item.final_rate ? formatCurrency(item.final_rate) : 'N/A'}</td>
                                            <td>${getStatusBadge(item.outcome)}</td>
                                            <td><span class="${getSentimentClass(item.sentiment)}">${item.sentiment || 'N/A'}</span></td>
                                            <td>${formatTimestamp(item.started_at)}</td>
                                        </tr>
                                    `).join('')}
                                </tbody>
                            </table>
                        `;
                        document.getElementById('recent-content').innerHTML = tableHTML;
                    } else {
                        document.getElementById('recent-content').innerHTML = '<p class="loading">No recent sessions found</p>';
                    }
                    
                } catch (error) {
                    console.error('Error loading dashboard:', error);
                    document.getElementById('stats-content').innerHTML = '<p style="color: #e53e3e;">Error loading data</p>';
                    document.getElementById('outcomes-content').innerHTML = '<p style="color: #e53e3e;">Error loading data</p>';
                    document.getElementById('sentiment-content').innerHTML = '<p style="color: #e53e3e;">Error loading data</p>';
                    document.getElementById('pricing-content').innerHTML = '<p style="color: #e53e3e;">Error loading data</p>';
                    document.getElementById('recent-content').innerHTML = '<p style="color: #e53e3e;">Error loading data</p>';
                }
            }
            
            // Load dashboard on page load
            loadDashboard();
            
            // Auto-refresh every 30 seconds
            setInterval(loadDashboard, 30000);
        </script>
    </body>
    </html>
    """