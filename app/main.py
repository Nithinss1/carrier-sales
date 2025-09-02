# app/main.py
import os
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Carrier Sales API (smoke)")

API_KEY = os.getenv("API_KEY", "devkey")  # default for local testing

def require_key(req: Request):
    if req.headers.get("x-api-key") != API_KEY:
        raise HTTPException(status_code=401, detail="invalid api key")

@app.get("/")
def root():
    return {"message": "API is working"}

@app.get("/health")
def health():
    return {
        "status": "healthy",
        "version": "2.0.1-ci-test",
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }

@app.post("/api/v1/match_loads")
async def match_loads(request: Request):
    require_key(request)
    body = await request.json()
    # Echo back the body so we can verify payloads end-to-end
    return {"received": body, "test": "success"}
