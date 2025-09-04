import os
import re
import logging
from typing import Optional, Dict, Any
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv
from time import time
from threading import Lock

# --- Boot ---
load_dotenv()
API_KEY = os.getenv("API_KEY", "")
app = FastAPI()
logger = logging.getLogger("uvicorn.error")

SESSION: Dict[str, Dict[str, Any]] = {}
SESSION_LOCK = Lock()

def _get_session(cid: Optional[str]) -> Dict[str, Any]:
    if not cid:
        return {}
    with SESSION_LOCK:
        s = SESSION.get(cid)
        if not s:
            s = {"created": time()}
            SESSION[cid] = s
        return s

# --- Models ---
class WebhookPayload(BaseModel):
    message: Optional[str] = ""        # transcripts can be empty
    caller_id: Optional[str] = None
    data: Optional[Dict[str, Any]] = None  # can carry structured MC, etc.

class MCVerificationRequest(BaseModel):
    mc: str

class SearchRequest(BaseModel):
    equipment_type: str
    origin: str

# --- Helpers ---
def require_key(x_api_key: Optional[str]):
    if not API_KEY:
        raise HTTPException(status_code=500, detail="server api key not set")
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid api key")

def _collapse_spaced_digits(text: str) -> str:
    """
    Collapse digit sequences with separators like:
    '317 7404', '317-7404', '3 1 7-7_4.0 4' -> '3177404'
    Only affects runs that look like 4–8 digits with optional separators.
    """
    return re.sub(
        r"(?:\d[\s\-\._]?){4,8}",
        lambda m: re.sub(r"\D", "", m.group(0)),
        text,
    )

def extract_mc_number_from_data(data: Optional[Dict[str, Any]]) -> Optional[str]:
    if not data:
        return None
    for key in ("mc", "MC", "mc_number", "motor_carrier", "motorCarrier", "motorCarrierNumber"):
        v = data.get(key)
        if isinstance(v, (int, float)):
            v = str(int(v))
        if isinstance(v, str):
            digits = re.sub(r"\D", "", v)
            if 4 <= len(digits) <= 7:
                return digits
    return None

def extract_mc_number(text: str, data: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """
    Robust MC extraction:
      - checks structured payload first,
      - then tries forgiving patterns in message/transcript,
      - supports 'MC# 123456', 'my mc is 123456', '123456 mc',
        and lone 6–7 digit tokens.
    """
    mc_from_data = extract_mc_number_from_data(data)
    if mc_from_data:
        return mc_from_data

    if not text:
        return None

    txt = _collapse_spaced_digits(text).strip()

    patterns = [
        r"(?:\bmc\b|\bmotor\s*carrier\b)\s*(?:number|#)?\s*(?:is|:)?\s*(\d{4,7})",
        r"(\d{4,7})\s*(?:mc|motor\s*carrier)\b",
        r"\bmc[#:\s]*?(\d{4,7})\b",
        r"(?<!\d)(\d{6,7})(?!\d)",  # last resort
    ]
    for p in patterns:
        m = re.search(p, txt, flags=re.IGNORECASE)
        if m:
            mc = m.group(1)
            if 4 <= len(mc) <= 7:
                return mc
    return None

def extract_equipment_type(text: str) -> Optional[str]:
    if not text:
        return None
    t = text.lower()
    mapping = [
        (["dry van", "dryvan", "dry", "van"], "Dry Van"),
        (["reefer", "refrigerated", "temp control", "temp-controlled"], "Reefer"),
        (["flatbed", "flat bed"], "Flatbed"),
        (["stepdeck", "step deck", "step-deck"], "Stepdeck"),
        (["conestoga"], "Conestoga"),
        (["box", "box truck", "26ft", "26 ft", "straight truck"], "Box Truck"),
        (["power only", "power-only", "poweronly"], "Power Only"),
        (["hotshot", "hot shot"], "Hotshot"),
        (["sprinter"], "Sprinter Van"),
    ]
    for keys, label in mapping:
        if any(k in t for k in keys):
            return label
    return None

US_STATE_ABBR = r"(?:A[LKZR]|C[AOT]|D[EC]|F[LM]|G[AU]|H[IW]|I[ADLN]|K[SY]|L[A]|M[ADEINOPST]|N[CDEHJMVY]|O[HKR]|P[A]|R[IL]|S[CD]|T[NX]|U[T]|V[AIT]|W[AIVY])"

def extract_location(text: str) -> Optional[str]:
    if not text:
        return None
    pats = [
        rf"(?:from|in|at|around)\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*,\s*{US_STATE_ABBR})",
        r"([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*)\s*,\s*"+US_STATE_ABBR,
        r"(?:from|in|at|around)\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*)",
    ]
    for p in pats:
        m = re.search(p, text, flags=re.IGNORECASE)  # case-insensitive
        if m:
            return m.group(1).strip()
    return None

# --- Stable envelope for HappyRobot Root field ---
BASE_RESULT = {
    "action": None,
    "status": None,
    "message": None,
    "response": None,
    "mc_number": None,
    "carrier_tier": None,
    "risk_score": None,
    "next_step": None,
    "load_id": None,
    "rate": None,
    "load_details": None,
}

def make_result(**kwargs):
    out = BASE_RESULT.copy()
    out.update(kwargs)
    return {"result": out}

# --- Placeholder business logic ---
async def verify_carrier_enhanced(request: MCVerificationRequest, x_api_key: str):
    class CarrierIntel:
        def __init__(self, eligible: bool, tier: str, score: int):
            self.eligible = eligible
            self.carrier_tier = tier
            self.risk_score = score

    if request.mc in {"000000", "123456"}:
        return CarrierIntel(False, "bronze", 20)
    score = 50 + (int(request.mc[-2:]) % 50)
    tier = "platinum" if score >= 90 else "gold" if score >= 75 else "silver"
    return CarrierIntel(True, tier, score)

def match_loads_intelligent(request: SearchRequest, carrier_mc: str, x_api_key: str):
    class LoadMatches:
        def __init__(self, loads):
            self.loads = loads
            self.total_matches = len(loads)

    loads = [{
        "load_id": "L-"+carrier_mc[-4:],
        "origin": request.origin,
        "destination": "Atlanta, GA",
        "market_adjusted_rate": 1500,
        "pickup_datetime": "2025-09-04T08:00:00"
    }]
    return LoadMatches(loads)

# --- Endpoints ---
@app.get("/health")
def health():
    return {"ok": True, "version": "mc-extractor-v2"}

@app.post("/api/v1/webhook/carrier_call")
async def handle_carrier_call_webhook(
    payload: WebhookPayload,
    x_api_key: Optional[str] = Header(None, alias="x-api-key")
):
    require_key(x_api_key)
    try:
        sess = _get_session(payload.caller_id)
        mc_number = extract_mc_number(payload.message or "", payload.data) or sess.get("mc")
        if not mc_number:
            return make_result(
                status="error",
                action="request_clarification",
                message="Could not extract MC number from message",
                response="I'm sorry, I couldn't find your MC number. Could you please say it again? For example, 'My MC number is 123456'",
            )

        verification_request = MCVerificationRequest(mc=mc_number)
        carrier_intel = await verify_carrier_enhanced(verification_request, x_api_key)

        # --- Store session info ---
        if payload.caller_id:
            sess["mc"] = mc_number
            sess["carrier_tier"] = carrier_intel.carrier_tier
            sess["risk_score"] = carrier_intel.risk_score

        if not carrier_intel.eligible:
            return make_result(
                status="not_eligible",
                action="end_call",
                mc_number=mc_number,
                message=(f"Thanks. I see your MC {mc_number}, but we can’t proceed while your authority is inactive. "
                         "Please reach back out once it’s active."),
            )

        response_message = f"Great! I have your MC {mc_number} and you're approved. "
        if carrier_intel.carrier_tier == "platinum":
            response_message += "You’re platinum—let me pull premium loads. "
        elif carrier_intel.carrier_tier == "gold":
            response_message += "As a gold-tier carrier, I have excellent options. "
        response_message += "What equipment are you running, and where are you looking to pick up?"

        return make_result(
            status="verified",
            action="collect_equipment_info",
            message=response_message,
            next_step="load_matching",
            mc_number=mc_number,
            carrier_tier=carrier_intel.carrier_tier,
            risk_score=carrier_intel.risk_score,
        )
    except Exception:
        logger.exception("Webhook processing failed")
        return make_result(
            status="error",
            action="transfer_to_human",
            message="I'm experiencing technical difficulties. Let me transfer you to one of our representatives.",
        )

@app.post("/api/v1/webhook/equipment_info")
async def handle_equipment_info(
    payload: WebhookPayload,
    mc_number: str,
    x_api_key: Optional[str] = Header(None, alias="x-api-key")
):
    require_key(x_api_key)
    try:
        sess = _get_session(payload.caller_id)
        equipment_type = extract_equipment_type(payload.message or "")
        origin = extract_location(payload.message or "")

        # --- Store session info ---
        if payload.caller_id:
            if equipment_type:
                sess["equipment_type"] = equipment_type
            if origin:
                sess["origin"] = origin

        if not equipment_type:
            return make_result(
                status="need_equipment",
                action="request_equipment_type",
                message="What type of equipment are you running? Dry van, reefer, or flatbed? (You can also say stepdeck, box truck, power-only, hotshot, etc.)",
            )
        if not origin:
            return make_result(
                status="need_location",
                action="request_pickup_location",
                message=f"Perfect, I have {equipment_type}. What city and state are you looking to pick up from? (e.g., Dallas, TX)",
            )

        search_request = SearchRequest(equipment_type=equipment_type, origin=origin)
        load_matches = match_loads_intelligent(search_request, mc_number, x_api_key)

        if load_matches.total_matches == 0:
            return make_result(
                status="no_matches",
                action="expand_search",
                message=f"I don't have any {equipment_type.lower()} loads out of {origin} right now. Can you run within 50 miles so I can expand the search?",
            )

        best_load = load_matches.loads[0]
        rate = best_load.get("market_adjusted_rate") or best_load.get("loadboard_rate")
        response_message = (
            f"I have a {equipment_type.lower()} load: {best_load['origin']} ➜ {best_load['destination']} "
            f"for ${rate}. Pickup {best_load.get('pickup_datetime', 'soon')}. Are you interested?"
        )

        return make_result(
            status="load_presented",
            action="await_response",
            message=response_message,
            load_id=best_load.get("load_id"),
            rate=rate,
            load_details=best_load,
        )
    except Exception:
        logger.exception("Equipment info processing failed")
        return make_result(
            status="error",
            action="transfer_to_human",
            message="Let me get a live agent to help you with this.",
        )
