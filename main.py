from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
import os

app = FastAPI(title="StundenPlaner API")

# CORS – erlaubt Anfragen vom Frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In Produktion: nur deine Frontend-URL
    allow_methods=["POST"],
    allow_headers=["*"],
)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

class GenerateRequest(BaseModel):
    teachers: list
    classes: list
    locked_entries: list = []

@app.get("/")
def health():
    return {"status": "ok", "service": "StundenPlaner API"}

@app.post("/api/generate")
async def generate_schedule(req: GenerateRequest):
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="API Key nicht konfiguriert")

    DAYS = ["Montag","Dienstag","Mittwoch","Donnerstag","Freitag"]
    HOURS = ["07:45","08:30","09:30","10:15","11:15","12:00","13:00","13:45"]

    locked_section = ""
    if req.locked_entries:
        import json
        locked_section = f"GESPERRTE EINTRÄGE (unveränderlich): {json.dumps(req.locked_entries, ensure_ascii=False)}"

    import json
    prompt = f"""Du bist ein Stundenplan-Solver (Sek I, Klassenverband).

LEHRKRÄFTE: {json.dumps([{"name":t["name"],"subjects":t["subjects"],"deputat":t["deputat"]} for t in req.teachers], ensure_ascii=False)}
KLASSEN: {json.dumps(req.classes, ensure_ascii=False)}
TAGE: {json.dumps(DAYS, ensure_ascii=False)}
ZEITSLOTS: {json.dumps(HOURS, ensure_ascii=False)}
{locked_section}

Erstelle einen vollständigen Stundenplan. Antworte NUR mit JSON-Array:
[{{"day":"Montag","time":"07:45","class":"5a","subject":"Mathematik","teacher":"Fr. Müller"}}]

Regeln:
1. Lehrkraft nie doppelt im selben Slot
2. Klasse nie doppelt im selben Slot
3. Lehrkraft unterrichtet nur ihre Fächer
4. Stundenzahlen exakt wie im curriculum
5. Gesperrte Einträge unverändert übernehmen
6. Kernfächer (Mathe,Deutsch) möglichst nicht Std 7-8
7. Gleichmäßige Verteilung über die Woche"""

    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": prompt}]
            }
        )

    if response.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Anthropic Fehler: {response.text}")

    data = response.json()
    text = "".join(b.get("text","") for b in data.get("content",[]))
    clean = text.replace("```json","").replace("```","").strip()

    import re
    try:
        schedule = json.loads(clean)
    except:
        match = re.search(r'\[[\s\S]*\]', clean)
        if match:
            schedule = json.loads(match.group())
        else:
            raise HTTPException(status_code=500, detail="JSON-Parse fehlgeschlagen")

    return {"schedule": schedule, "count": len(schedule)}
