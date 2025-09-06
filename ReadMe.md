
# HappyRobot Inbound (Option B)

Low-code + tiny API for the FDE challenge (Inbound Carrier Sales).

## Endpoints
- `POST /verify_carrier` — uses upstream verify service if configured, else local heuristic.
- `POST /search_loads` — returns 1–3 loads from `app/loads.json` by simple filters.
- `POST /evaluate_offer` — 3-round bounded negotiation policy.
- `POST /classify_and_log` — stores a call record to SQLite; KPIs exposed at `/metrics`.
- `GET /dashboard` — quick Chart.js dashboard (pulls `/metrics`).

## Env Vars
- `SERVICE_API_KEY` — what HappyRobot must send in `x-api-key` for all endpoints.
- `FMCSA_API_KEY` — your FMCSA key (optional; not used by default).
- `CARRIER_UPSTREAM_URL` — optional: existing verify API endpoint (e.g., your Railway service).
- `CARRIER_UPSTREAM_HEADER` — header name for upstream key (default `x-api-key`).
- `CARRIER_UPSTREAM_KEY` — value for upstream key (if required).
- `DB_PATH` — SQLite file path (default `data.db`).

> **Security note:** never hardcode secrets in code or git. Use env vars.

## Local Run
```bash
docker build -t hr-inbound .
docker run -p 8080:8080 -e SERVICE_API_KEY=supersecret hr-inbound
# health
curl localhost:8080/healthz
```

## Quick Tests
```bash
# 1) verify
curl -s -X POST localhost:8080/verify_carrier   -H "x-api-key: supersecret" -H "Content-Type: application/json"   -d '{"mc":"123456"}' | jq

# 2) search
curl -s -X POST localhost:8080/search_loads   -H "x-api-key: supersecret" -H "Content-Type: application/json"   -d '{"origin":"Dallas, TX","destination":"Atlanta, GA",
       "pickup_start":"2025-09-05T00:00:00","pickup_end":"2025-09-05T23:59:59",
       "equipment_type":"Dry Van","max_results":3}' | jq

# 3) evaluate
curl -s -X POST localhost:8080/evaluate_offer   -H "x-api-key: supersecret" -H "Content-Type: application/json"   -d '{"load_id":"DL-ATL-001","listed_rate":1500,"carrier_offer":1300,"tier":"silver","round":1}' | jq

# 4) log
curl -s -X POST localhost:8080/classify_and_log   -H "x-api-key: supersecret" -H "Content-Type: application/json"   -d '{"call_id":"run-001","mc":"123456","load_id":"DL-ATL-001","listed_rate":1500,"final_rate":1450,"rounds":2,"outcome":"agreed","sentiment":"positive"}' | jq

# 5) metrics
curl -s localhost:8080/metrics | jq
# Visit http://localhost:8080/dashboard
```

## HappyRobot Webhook Mappings

**Headers (all webhook nodes):**
```
x-api-key: {{YOUR_SERVICE_API_KEY}}
Content-Type: application/json
```

### Webhook: Verify Carrier
**URL:** https://<your-host>/verify_carrier
**Body:**
{
  "mc": "{{extract.mc}}",
  "caller_number": "{{call.caller_number}}"
}
Branch: if eligible == false -> end; else -> continue

### Webhook: Search Loads
**URL:** https://<your-host>/search_loads
**Body:**
{
  "origin": "{{capture.origin}}",
  "destination": "{{capture.destination}}",
  "pickup_start": "{{capture.pickup_start}}",
  "pickup_end": "{{capture.pickup_end}}",
  "equipment_type": "{{capture.equipment_type}}",
  "max_results": 3
}
Use output: pitch {{webhook.loads[0]}} fields

### Webhook: Evaluate Offer
**URL:** https://<your-host>/evaluate_offer
**Body:**
{
  "load_id": "{{selected.load_id}}",
  "listed_rate": "{{selected.loadboard_rate}}",
  "carrier_offer": "{{carrier.offer_amount}}",
  "tier": "{{verify.carrier_tier}}",
  "miles": "{{selected.miles}}",
  "equipment_type": "{{selected.equipment_type}}",
  "round": "{{context.round_count}}"
}
Loop: limit 3 rounds. Accept if decision == "accept"; else quote next_offer.

### Webhook: Classify & Log
**URL:** https://<your-host>/classify_and_log
**Body:**
{
  "call_id": "{{run.id}}",
  "mc": "{{extract.mc}}",
  "load_id": "{{selected.load_id}}",
  "listed_rate": "{{selected.loadboard_rate}}",
  "final_rate": "{{context.final_rate}}",
  "rounds": "{{context.round_count}}",
  "outcome": "{{outcome.label}}",
  "sentiment": "{{sentiment.label}}",
  "timestamp": "{{now}}",
  "extra": {
    "origin": "{{selected.origin}}",
    "destination": "{{selected.destination}}",
    "pickup_datetime": "{{selected.pickup_datetime}}",
    "delivery_datetime": "{{selected.delivery_datetime}}"
  }
}

## Deploy
Any Docker host (Railway/Fly/Render). Set env vars, expose port 8080, paste public URLs into HappyRobot webhook nodes.
