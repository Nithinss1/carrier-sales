# app/fmcsa.py
"""
FMCSA QCMobile API client.
Docs: https://mobile.fmcsa.dot.gov/QCDevsite/docs/qcApi
Auth: append ?webKey=YOUR_KEY to every request.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Dict, Optional, Tuple, TypedDict

import requests


BASE = "https://mobile.fmcsa.dot.gov/qc/services"
DEFAULT_TIMEOUT = 10  # seconds
RETRIES = 2           # simple retry on transient errors


class VerifyResult(TypedDict, total=False):
    mc: str
    dot: Optional[str]
    eligible: bool
    status: str               # "authorized", "not_authorized", "out_of_service", "unknown"
    raw: Dict[str, Any]       # raw snippets useful for debugging / audit


class FmcsaClient:
    def __init__(self, webkey: Optional[str] = None, session: Optional[requests.Session] = None):
        self.webkey = webkey or os.getenv("FMCSA_WEBKEY")
        if not self.webkey:
            raise RuntimeError("FMCSA_WEBKEY is not set")
        self.session = session or requests.Session()

    # ---- public API ---------------------------------------------------------

    def verify_mc(self, mc: str) -> VerifyResult:
        """
        Given an MC (docket) number, determine if the carrier is eligible to operate.
        Logic:
          1) Resolve MC -> USDOT via /carriers/docket-number/:mc
          2) Pull authority + OOS/operate flags via:
              - /carriers/:dot
              - /carriers/:dot/authority
              - /carriers/:dot/oos   (best-effort)
          3) Eligible if:
              allowToOperate == 'Y'
              AND outOfService != 'Y'
              AND (any of common/contract/property authority is ACTIVE/AUTHORIZED)
        Returns a dict with eligibility + reason and a few raw fields for audit.
        """
        norm_mc = _digits(mc)
        out: VerifyResult = {"mc": norm_mc, "dot": None, "eligible": False, "status": "unknown", "raw": {}}

        # 1) MC -> DOT
        res = self._get_json(f"{BASE}/carriers/docket-number/{norm_mc}")
        dot = _extract_dot_from_docket(res)
        out["raw"]["docketLookup"] = res
        if not dot:
            out["status"] = "not_found"
            return out

        out["dot"] = dot

        # 2) Core carrier record (has allowToOperate / outOfService when available)
        core = self._get_json(f"{BASE}/carriers/{dot}")
        out["raw"]["carrier"] = core
        allow = _get_case_insensitive(core, ["carrier", "allowToOperate"]) or _get_case_insensitive(core, ["allowToOperate"])
        oos = _get_case_insensitive(core, ["carrier", "outOfService"]) or _get_case_insensitive(core, ["outOfService"])

        # 3) Authority details (authorized for property/common/contract)
        auth = self._get_json(f"{BASE}/carriers/{dot}/authority")
        out["raw"]["authority"] = auth
        statuses = _collect_authority_statuses(auth)

        # 4) Out-of-service details (best effort; not always returned)
        try:
            oos_detail = self._get_json(f"{BASE}/carriers/{dot}/oos")
            out["raw"]["oos"] = oos_detail
        except Exception:
            pass  # not fatal

        # Evaluate eligibility
        allow_ok = str(allow).upper() == "Y"
        oos_ok = str(oos).upper() != "Y"
        has_active = any(s in {"ACTIVE", "AUTHORIZED"} for s in statuses)

        if allow_ok and oos_ok and has_active:
            out["eligible"] = True
            out["status"] = "authorized"
        elif str(oos).upper() == "Y":
            out["eligible"] = False
            out["status"] = "out_of_service"
        else:
            out["eligible"] = False
            out["status"] = "not_authorized"

        return out

    # ---- internals ----------------------------------------------------------

    def _get_json(self, url: str) -> Dict[str, Any]:
        # Append webKey on every request
        sep = "&" if "?" in url else "?"
        full = f"{url}{sep}webKey={self.webkey}"

        last_exc = None
        for attempt in range(RETRIES + 1):
            try:
                r = self.session.get(full, timeout=DEFAULT_TIMEOUT)
                if r.status_code == 401:
                    raise RuntimeError("FMCSA 401 Unauthorized (check webKey)")
                r.raise_for_status()
                return r.json()
            except Exception as e:
                last_exc = e
                if attempt < RETRIES and _is_retryable(e):
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise
        assert False, last_exc  # unreachable

    @staticmethod
    def _example() -> None:
        """
        Quick manual test:
            export FMCSA_WEBKEY=...
            python -c 'from app.fmcsa import FmcsaClient; print(FmcsaClient().verify_mc("123456"))'
        """
        pass


# ---- helpers ----------------------------------------------------------------

def _digits(s: str) -> str:
    return "".join(ch for ch in s if ch.isdigit())

def _is_retryable(exc: Exception) -> bool:
    # naive retry predicate for transient network/server hiccups
    return isinstance(exc, requests.Timeout) or "502" in str(exc) or "503" in str(exc) or "504" in str(exc)

def _extract_dot_from_docket(payload: Dict[str, Any]) -> Optional[str]:
    """
    The docket lookup returns a list of carrier details; structure can vary.
    We try common shapes seen in FMCSA responses.
    """
    if not payload:
        return None

    # Common shapes to try:
    # 1) {"content": [{"carrier": {"dotNumber": 12345, ...}}]}
    # 2) [{"carrier": {"dotNumber": 12345, ...}}, ...]
    # 3) {"carrier": {"dotNumber": 12345}}
    paths = [
        (["content", 0, "carrier", "dotNumber"],),
        (["content", 0, "dotNumber"],),
        (["carrier", "dotNumber"],),
        (["dotNumber"],),
        (["items", 0, "carrier", "dotNumber"],),
    ]
    for path in paths:
        v = _dig(payload, path[0])
        if v:
            return str(v)
    # Fallback: scan for first integer-ish field named dotNumber
    if isinstance(payload, dict):
        for k, v in payload.items():
            if isinstance(v, (dict, list)):
                found = _scan_for_key(v, "dotNumber")
                if found:
                    return str(found)
    return None

def _collect_authority_statuses(payload: Dict[str, Any]) -> set[str]:
    """
    Collects authority status fields that often appear in /authority responses:
      - commonAuthorityStatus
      - contractAuthorityStatus
      - brokerAuthorityStatus
      - authorizedForProperty (Y/N)
    """
    statuses: set[str] = set()
    if not payload:
        return statuses

    # Normalize to a list of records
    records: list[Dict[str, Any]] = []
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        # some responses wrap in {"content": [ ... ]}
        if isinstance(payload.get("content"), list):
            records = payload["content"]
        else:
            records = [payload]

    keys = [
        "commonAuthorityStatus",
        "contractAuthorityStatus",
        "brokerAuthorityStatus",
        "authorizedForProperty",
        "authorizedForPassenger",
        "authorizedForHouseholdGoods",
    ]
    for rec in records:
        for k in keys:
            val = _get_case_insensitive(rec, [k])
            if val:
                statuses.add(str(val).upper())
    return statuses

def _get_case_insensitive(obj: Dict[str, Any], path: list[Any]) -> Any:
    cur: Any = obj
    for key in path:
        if isinstance(key, int):
            if isinstance(cur, list) and 0 <= key < len(cur):
                cur = cur[key]
            else:
                return None
        else:
            if isinstance(cur, dict):
                # try case-insensitive match
                lowered = {k.lower(): k for k in cur.keys()}
                lk = key.lower()
                if lk in lowered:
                    cur = cur[lowered[lk]]
                else:
                    return None
            else:
                return None
    return cur

def _dig(obj: Any, path: list[Any]) -> Any:
    cur = obj
    for p in path:
        if isinstance(p, int):
            if isinstance(cur, list) and 0 <= p < len(cur):
                cur = cur[p]
            else:
                return None
        else:
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                return None
    return cur

def _scan_for_key(obj: Any, target: str) -> Any:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() == target.lower():
                return v
            found = _scan_for_key(v, target)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _scan_for_key(item, target)
            if found is not None:
                return found
    return None
