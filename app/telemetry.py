import os, sqlite3, time, json, uuid
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Body, Header, HTTPException, Depends

# -----------------------------
# Config
# -----------------------------
DB_PATH = os.getenv("LOG_DB", "telemetry.db")   # set to /data/telemetry.db if using a Railway Volume
API_KEY = os.getenv("API_KEY", "supersecret123")
PROTECT_LOGS = os.getenv("PROTECT_LOGS", "0") == "1"  # set to 1 to require x-api-key on /log/*

def require_key(x_api_key: Optional[str] = Header(None)):
    if PROTECT_LOGS and x_api_key != API_KEY:
        raise HTTPException(401, "Unauthorized")

# -----------------------------
# SQLite setup
# -----------------------------
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.execute("PRAGMA journal_mode=WAL;")
conn.execute("PRAGMA synchronous=NORMAL;")
# Add new columns for session summary and ended_at if not present

def _cols(table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}

def _ensure_schema():
    sess_cols = _cols("sessions")
    for col, ddl in [
        ("ended_at",  "ALTER TABLE sessions ADD COLUMN ended_at REAL"),
        ("caller",    "ALTER TABLE sessions ADD COLUMN caller TEXT"),
        ("outcome",   "ALTER TABLE sessions ADD COLUMN outcome TEXT"),
        ("final_rate","ALTER TABLE sessions ADD COLUMN final_rate REAL"),
        ("load_id",   "ALTER TABLE sessions ADD COLUMN load_id TEXT"),
    ]:
        if col not in sess_cols:
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError:
                pass  # ignore if already added elsewhere
    conn.commit()

_ensure_schema()

conn.execute("""
CREATE TABLE IF NOT EXISTS sessions(
  session_id TEXT PRIMARY KEY,
  started_at REAL,
  ended_at REAL,
  caller TEXT,
  outcome TEXT,
  final_rate REAL,
  load_id TEXT
)
""")
conn.execute("""
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT,
  ts REAL,
  type TEXT,
  data TEXT
)
""")
conn.commit()

# -----------------------------
# Helper functions (import & call from your endpoints)
# -----------------------------
def start_session(caller: Optional[str] = None, session_id: Optional[str] = None) -> str:
    sid = session_id or str(uuid.uuid4())
    conn.execute(
        "INSERT OR IGNORE INTO sessions(session_id, started_at, caller) VALUES (?,?,?)",
        (sid, time.time(), caller),
    )
    conn.commit()
    return sid

def log_event(session_id: str, event_type: str, data: Dict[str, Any]):
    # auto-create session if unknown (useful after redeploys)
    conn.execute(
        "INSERT OR IGNORE INTO sessions(session_id, started_at) VALUES (?,?)",
        (session_id, time.time()),
    )
    conn.execute(
        "INSERT INTO events(session_id, ts, type, data) VALUES (?,?,?,?)",
        (session_id, time.time(), event_type, json.dumps(data)),
    )
    conn.commit()

def end_session(session_id: str, summary: Optional[Dict[str, Any]] = None):
    if summary:
        # Update session with summary fields and ended_at
        conn.execute(
            "UPDATE sessions SET ended_at=?, outcome=?, final_rate=?, load_id=? WHERE session_id=?",
            (
                time.time(),
                summary.get("outcome"),
                summary.get("final_rate"),
                summary.get("load_id"),
                session_id,
            )
        )
        conn.commit()
        log_event(session_id, "summary", summary)
    else:
        # Even if no summary, mark as ended
        conn.execute(
            "UPDATE sessions SET ended_at=? WHERE session_id=?",
            (time.time(), session_id)
        )
        conn.commit()

# Convenience wrappers you already used
def log_verify_result(sid, mc, status, eligible, tier, risk_score):
    log_event(sid, "verify_result", {
        "mc": mc, "status": status, "eligible": bool(eligible),
        "carrier_tier": tier, "risk_score": risk_score
    })

def log_loads_pitched(sid, loads: List[Dict[str, Any]]):
    log_event(sid, "loads_pitched", {"loads": loads})

def log_negotiation_round(sid, round_num, load_id, listed_rate, our_offer, carrier_offer, decision, next_offer, cap_rate):
    log_event(sid, "negotiation_round", {
        "round": round_num, "load_id": load_id, "listed_rate": listed_rate,
        "our_offer": our_offer, "carrier_offer": carrier_offer,
        "decision": decision, "next_offer": next_offer, "cap_rate": cap_rate
    })

def log_outcome(sid, outcome, final_rate=None, load_id=None):
    log_event(sid, "outcome", {"outcome": outcome, "final_rate": final_rate, "load_id": load_id})

def log_sentiment(sid, label, score=None):
    log_event(sid, "sentiment", {"label": label, "score": score})

# -----------------------------
# Router (optional HTTP endpoints for dashboard)
# -----------------------------
router = APIRouter(prefix="/log", tags=["telemetry"],
                   dependencies=[Depends(require_key)] if PROTECT_LOGS else [])

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
    sid = body["session_id"]
    log_event(sid, body.get("event_type", "event"), body.get("data", {}))
    return {"ok": True}

@router.get("/events/{session_id}")
def get_events(session_id: str):
    s = conn.execute(
        "SELECT session_id, started_at, ended_at, caller, outcome, final_rate, load_id FROM sessions WHERE session_id=?",
        (session_id,)
    ).fetchone()
    evs = conn.execute(
        "SELECT ts, type, data FROM events WHERE session_id=? ORDER BY id ASC",
        (session_id,)
    ).fetchall()
    # Build session payload + nested summary for compatibility
    summary = None
    if s and (s[4] is not None or s[5] is not None or s[6] is not None):
        summary = {
            "outcome": s[4],
            "final_rate": s[5],
            "load_id": s[6],
        }
    session_payload = {
        "session_id": s[0] if s else None,
        "started_at": s[1] if s else None,
        "ended_at": s[2] if s else None,
        "caller": s[3] if s else None,
        "outcome": s[4] if s else None,
        "final_rate": s[5] if s else None,
        "load_id": s[6] if s else None,
        "summary": summary,
    } if s else None
    return {
        "session": session_payload,
        "events": [{"ts": r[0], "type": r[1], "data": json.loads(r[2])} for r in evs]
    }

# ---------- Dashboard endpoints ----------
@router.get("/summary")
def log_summary():
    # totals
    tot = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] or 0

    # accept count
    acc = conn.execute("""
        SELECT COUNT(DISTINCT session_id) FROM events
        WHERE type='outcome' AND json_extract(data,'$.outcome')='accept'
    """).fetchone()[0] or 0

    # avg rounds
    avg_rounds = conn.execute("""
        SELECT AVG(cnt) FROM (
          SELECT COUNT(*) AS cnt FROM events WHERE type='negotiation_round' GROUP BY session_id
        )
    """).fetchone()[0] or 0

    # outcome mix
    def count_outcome(o): 
        return conn.execute("""
            SELECT COUNT(DISTINCT session_id) FROM events
            WHERE type='outcome' AND json_extract(data,'$.outcome')=?
        """, (o,)).fetchone()[0] or 0
    outcome_counts = {
        "accept": count_outcome("accept"),
        "decline": count_outcome("decline"),
        "callback": count_outcome("callback"),
        "counter": count_outcome("counter"),
        "info_only": count_outcome("info_only"),
    }

    # sentiment mix
    def count_sent(label):
        return conn.execute("""
          SELECT COUNT(DISTINCT session_id) FROM events
          WHERE type='sentiment' AND json_extract(data,'$.label')=?
        """,(label,)).fetchone()[0] or 0
    sentiment_counts = {
        "positive": count_sent("positive"),
        "neutral":  count_sent("neutral"),
        "negative": count_sent("negative"),
    }

    # pricing deltas
    deltas = conn.execute("""
        SELECT AVG(final_rate - listed_rate) AS d_abs,
               AVG((final_rate - listed_rate) / NULLIF(listed_rate,0)) AS d_pct
        FROM (
          SELECT
            CAST(json_extract(o.data,'$.final_rate') AS REAL) AS final_rate,
            CAST(json_extract(lp.data,'$.loads[0].loadboard_rate') AS REAL) AS listed_rate
          FROM events o
          JOIN (
            SELECT session_id, MAX(id) AS id FROM events WHERE type='loads_pitched' GROUP BY session_id
          ) lp_idx ON lp_idx.session_id=o.session_id
          JOIN events lp ON lp.id=lp_idx.id
          WHERE o.type='outcome' AND json_extract(o.data,'$.final_rate') IS NOT NULL
        )
    """).fetchone()
    d_abs = round(deltas[0], 2) if deltas and deltas[0] is not None else 0.0
    d_pct = round(deltas[1], 3) if deltas and deltas[1] is not None else 0.0

    return {
        "totals": {"sessions": tot, "accept_rate": round((acc/tot), 3) if tot else 0.0, "avg_rounds": round(avg_rounds, 2)},
        "mix": {"outcomes": outcome_counts, "sentiment": sentiment_counts},
        "pricing": {"avg_delta_abs": d_abs, "avg_delta_pct": d_pct}
    }

@router.get("/recent")
def log_recent(limit: int = 10):
    rows = conn.execute("""
        WITH last_lp AS (
          SELECT session_id, MAX(id) id FROM events WHERE type='loads_pitched' GROUP BY session_id
        ),
        last_out AS (
          SELECT session_id, MAX(id) id FROM events WHERE type='outcome' GROUP BY session_id
        ),
        last_sent AS (
          SELECT session_id, MAX(id) id FROM events WHERE type='sentiment' GROUP BY session_id
        )
        SELECT s.session_id, s.started_at, s.ended_at,
               json_extract(lo.data,'$.outcome') AS outcome,
               json_extract(lo.data,'$.final_rate') AS final_rate,
               json_extract(lp.data,'$.loads[0].loadboard_rate') AS listed_rate,
               json_extract(lp.data,'$.loads[0].origin') || ' → ' ||
               json_extract(lp.data,'$.loads[0].destination') AS lane,
               json_extract(ls.data,'$.label') AS sentiment
        FROM sessions s
        LEFT JOIN last_lp tlp ON tlp.session_id=s.session_id
        LEFT JOIN events lp ON lp.id=tlp.id
        LEFT JOIN last_out to1 ON to1.session_id=s.session_id
        LEFT JOIN events lo ON lo.id=to1.id
        LEFT JOIN last_sent ts ON ts.session_id=s.session_id
        LEFT JOIN events ls ON ls.id=ts.id
        ORDER BY s.started_at DESC
        LIMIT ?
    """, (limit,)).fetchall()
    return {"items": [{
        "session_id": r[0], "started_at": r[1], "ended_at": r[2],
        "outcome": r[3], "final_rate": r[4], "listed_rate": r[5], "lane": r[6], "sentiment": r[7]
    } for r in rows]}