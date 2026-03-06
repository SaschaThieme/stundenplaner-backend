from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
import os
import json
import re

app = FastAPI(title="StundenPlaner API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
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
        locked_section = f"\nGESPERRTE EINTRÄGE (unveränderlich übernehmen): {json.dumps(req.locked_entries, ensure_ascii=False)}"

    prompt = f"""Du bist ein Stundenplan-Solver für eine deutsche Schule (Sek I, Klassenverband).

LEHRKRÄFTE: {json.dumps([{{"name":t["name"],"subjects":t["subjects"],"deputat":t["deputat"]}} for t in req.teachers], ensure_ascii=False)}
KLASSEN: {json.dumps(req.classes, ensure_ascii=False)}
TAGE: {json.dumps(DAYS, ensure_ascii=False)}
ZEITSLOTS: {json.dumps(HOURS, ensure_ascii=False)}{locked_section}

Regeln:
1. Lehrkraft nie doppelt im selben Slot
2. Klasse nie doppelt im selben Slot
3. Lehrkraft unterrichtet nur ihre Fächer
4. Stundenzahlen exakt wie im curriculum
5. Gesperrte Einträge unverändert übernehmen
6. Kernfächer (Mathe,Deutsch) möglichst nicht Std 7-8
7. Gleichmäßige Verteilung über die Woche

AUSGABE: Antworte AUSSCHLIESSLICH mit einem rohen JSON-Array.
Kein Text davor oder danach. Keine Erklärungen. Kein Markdown.
Beginne sofort mit [ und ende mit ]

[{{"day":"Montag","time":"07:45","class":"5a","subject":"Mathematik","teacher":"Fr. Müller"}}]"""

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
                "max_tokens": 8096,
                "messages": [{"role": "user", "content": prompt}]
            }
        )

    if response.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Anthropic Fehler: {response.text}")

    data = response.json()
    text = "".join(b.get("text","") for b in data.get("content",[]))
    clean = text.replace("```json","").replace("```","").strip()

    try:
        schedule = json.loads(clean)
    except:
        match = re.search(r'\[[\s\S]*\]', clean)
        if match:
            try:
                schedule = json.loads(match.group())
            except:
                raise HTTPException(status_code=500, detail=f"JSON-Parse fehlgeschlagen. Antwort: {clean[:300]}")
        else:
            raise HTTPException(status_code=500, detail=f"Kein JSON gefunden. Antwort: {clean[:300]}")

    return {"schedule": schedule, "count": len(schedule)}
