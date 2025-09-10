# HappyRobot Inbound Carrier Sales API

Low-code + FastAPI solution for the FDE challenge (Inbound Carrier Sales) with built-in telemetry and dashboard.

## Features

- **Carrier verification** with upstream service integration or local fallback
- **Load search** with flexible filtering from JSON data source
- **3-round negotiation engine** with bounded pricing logic
- **Real-time telemetry** tracking sessions, events, and outcomes
- **Interactive dashboard** with metrics visualization
- **Fully containerized** with Docker for easy deployment

## API Endpoints

### Core Operations
- `POST /verify_carrier` — Validates carrier MC numbers using upstream service or local heuristic
- `POST /search_loads` — Returns 1–3 loads from `app/loads.json` with smart filtering
- `POST /evaluate_offer` — Implements 3-round bounded negotiation policy with monotonic pricing
- `POST /classify_and_log` — Legacy endpoint for call record storage
- `GET /metrics` — Legacy metrics endpoint
- `GET /dashboard` — Interactive HTML dashboard with real-time data

### Telemetry & Analytics
- `POST /log/session/start` — Initialize new session tracking
- `POST /log/session/end` — Complete session with summary
- `POST /log/events` — Log arbitrary events during session
- `GET /log/events/{session_id}` — Retrieve session timeline
- `GET /log/summary` — Aggregate statistics and KPIs
- `GET /log/recent` — Recent session activity

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `API_KEY` | Authentication key for all endpoints | `supersecret123` |
| `FMCSA_API_KEY` | FMCSA API key (optional) | None |
| `CARRIER_UPSTREAM_URL` | External carrier verification service URL | None |
| `CARRIER_UPSTREAM_HEADER` | Header name for upstream authentication | `API_KEY` |
| `CARRIER_UPSTREAM_KEY` | Authentication value for upstream service | None |
| `DB_PATH` | SQLite database file path | `data.db` |
| `LOG_DB` | Telemetry database file path | `telemetry.db` |
| `PROTECT_LOGS` | Require auth for telemetry endpoints (`0` or `1`) | `0` |

**Security Note:** Never hardcode secrets in code or git. Always use environment variables for sensitive data.

## Local Development

### Docker (Recommended)

```bash
# Build the container
docker build -t hr-inbound .

# Run with environment variables
docker run -p 8080:8080 \
  -e API_KEY=your-secret-key \
  -e CARRIER_UPSTREAM_URL=https://your-verify-service.com/verify \
  -e CARRIER_UPSTREAM_KEY=your-upstream-key \
  hr-inbound

# Health check
curl localhost:8080/healthz
```

### Direct Python

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables
export API_KEY=your-secret-key

# Run the application
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

## API Testing Examples

### 1. Verify Carrier

```bash
curl -X POST localhost:8080/verify_carrier \
  -H "x-api-key: supersecret123" \
  -H "Content-Type: application/json" \
  -d '{"mc":"123456","caller_number":"+15551234567"}' | jq
```

### 2. Search Loads

```bash
curl -X POST localhost:8080/search_loads \
  -H "x-api-key: supersecret123" \
  -H "Content-Type: application/json" \
  -d '{
    "origin":"Dallas, TX",
    "destination":"Atlanta, GA",
    "pickup_start":"2025-09-05T00:00:00",
    "pickup_end":"2025-09-05T23:59:59",
    "equipment_type":"Dry Van",
    "max_results":3
  }' | jq
```

### 3. Evaluate Offer

```bash
curl -X POST localhost:8080/evaluate_offer \
  -H "x-api-key: supersecret123" \
  -H "Content-Type: application/json" \
  -d '{
    "load_id":"DL-ATL-001",
    "listed_rate":1500,
    "our_offer":1400,
    "carrier_offer":1300,
    "miles":850,
    "equipment_type":"Dry Van",
    "round":1
  }' | jq
```

### 4. View Dashboard

Visit `http://localhost:8080/dashboard` in your browser for real-time metrics and session tracking.

## HappyRobot Integration

### Webhook Configuration

**Global Headers (all webhook nodes):**
```json
{
  "x-api-key": "{{YOUR_API_KEY}}",
  "Content-Type": "application/json"
}
```

### Webhook: Verify Carrier
- **URL:** `https://carrier-sales-api-production.up.railway.app/verify_carrier`
- **Body:**
```json
{
  "mc": "{{extract.mc}}",
  "caller_number": "{{call.caller_number}}",
  "session_id": "{{run.id}}"
}
```
- **Branching:** If `eligible == false` → end call; else → continue

### Webhook: Search Loads
- **URL:** `https://carrier-sales-api-production.up.railway.app/search_loads`
- **Body:**
```json
{
  "origin": "{{capture.origin}}",
  "destination": "{{capture.destination}}",
  "pickup_start": "{{capture.pickup_start}}",
  "pickup_end": "{{capture.pickup_end}}",
  "equipment_type": "{{capture.equipment_type}}",
  "max_results": 3,
  "session_id": "{{webhook.session_id}}"
}
```
- **Usage:** Pitch `{{webhook.loads[0]}}` fields to carrier

### Webhook: Evaluate Offer
- **URL:** `https://carrier-sales-api-production.up.railway.app/evaluate_offer`
- **Body:**
```json
{
  "load_id": "{{selected.load_id}}",
  "listed_rate": "{{selected.loadboard_rate}}",
  "our_offer": "{{context.last_offer}}",
  "carrier_offer": "{{carrier.offer_amount}}",
  "miles": "{{selected.miles}}",
  "equipment_type": "{{selected.equipment_type}}",
  "round": "{{context.round_count}}",
  "session_id": "{{webhook.session_id}}"
}
```
- **Loop Logic:** Limit 3 rounds. If `decision == "accept"` → accept deal; else → quote `next_offer`

### Webhook: Session Summary (End of Call)
- **URL:** `https://carrier-sales-api-production.up.railway.app/log/session/end`
- **Body:**
```json
{
  "session_id": "{{webhook.session_id}}",
  "summary": {
    "outcome": "{{outcome.label}}",
    "final_rate": "{{context.final_rate}}",
    "load_id": "{{selected.load_id}}",
    "rounds": "{{context.round_count}}",
    "sentiment": "{{sentiment.label}}"
  }
}
```

**Replace with your deployed domain when you deploy your own instance.**

## Deployment

### Railway
1. Connect your GitHub repository
2. Set environment variables in Railway dashboard
3. Deploy automatically on git push
4. Use the provided Railway URL in HappyRobot webhooks

### Production Considerations
- Set strong `API_KEY` values
- Use persistent storage for SQLite databases (Railway Volumes, etc.)
- Enable `PROTECT_LOGS=1` for telemetry security
- Monitor `/log/summary` for performance metrics

## Database Schema

The application uses two SQLite databases:

**Main Database (`data.db`):**
- `calls` table for legacy call logging

**Telemetry Database (`telemetry.db`):**
- `sessions` table for session tracking
- `events` table for detailed event logging

Both databases are automatically created and migrated on startup.

## Negotiation Logic

The evaluate_offer endpoint implements a sophisticated 3-round negotiation strategy:

1. **Stable Cap Calculation:** Based on listed rate, equipment type, and distance
2. **Monotonic Pricing:** Never reduces offers within a session
3. **Round-Based Concessions:** Decreasing concession rates (35% → 25% → 20%)
4. **Final Round Strategy:** Move to cap if still negotiating

This ensures predictable, professional negotiation behavior while maximizing profitability.