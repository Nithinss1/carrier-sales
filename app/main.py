import os
from fastapi import FastAPI, Request, HTTPException
from dotenv import load_dotenv

load_dotenv()  # Loads .env file

app = FastAPI()
API_KEY = os.getenv("API_KEY")

@app.get("/")
def root():
    return {"message": "API is working"}

@app.get("/health")  
def health():
    return {"status": "ok"}

@app.post("/api/v1/match_loads")
async def match_loads(request: Request):
    api_key = request.headers.get("x-api-key")
    if api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid api key")
    try:
        body = await request.json()
        return {"received": body, "test": "success"}
    except Exception as e:
        return {"error": str(e)}