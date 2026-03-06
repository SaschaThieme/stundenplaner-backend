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
DAYS  = ["Montag","Dienstag","Mittwoch","Donnerstag","Freitag"]
HOURS = ["07:45","08:30","09:30","10:15","11:15","12:00","13:00","13:45"]

class GenerateRequest(BaseModel):
    teachers: list
    classes: list
    locked_entries: list = []

@app.get("/")
def health():
    return {"status": "ok", "service": "StundenPlaner API"}

async def generate_for_class(client, cls, teachers, existing_schedule, locked_entries):
    occupied = [f"{e['teacher']}|{e['day']}|{e['time']}" for e in existing_schedule]
    occupied_info = f"\nBEREITS BELEGTE LEHRER-SLOTS (nicht verwenden):\n{json.dumps(occupied, ensure_ascii=False)}" if occupied else ""
    locked_for_class = [e for e in locked_entries if e.get("class") == cls["name"]]
    locked_info = f"\nGESPERRTE EINTRÄGE: {json.dumps(locked_for_class, ensure_ascii=False)}" if locked_for_class else ""

    prompt = f"""Du bist ein Stundenplan-Solver für eine deutsche Schule (Sek I).
Erstelle den Stundenplan NUR für Klasse {cls["name"]}.

KLASSE: {json.dumps(cls, ensure_ascii=False)}
LEHRKRÄFTE: {json.dumps([{{"name":t["name"],"subjects":t["subjects"]}} for t in teachers], ensure_ascii=False)}
TAGE: {json.dumps(DAYS, ensure_ascii=False)}
ZEITSLOTS: {json.dumps(HOURS, ensure_ascii=False)}{occupied_info}{locked_info}

Regeln:
1. Lehrkraft nie doppelt im selben Tag/Zeit-Slot
2. Bereits belegte Lehrer-Slots NICHT verwenden
3. Lehrkraft unterrichtet nur ihre Fächer
4. Stundenzahlen exakt wie im curriculum
5. Kernfächer (Mathe,Deutsch) möglichst in Stunden 1-5
6. Gleichmäßige Verteilung über die Woche

AUSGABE: Nur rohes JSON-Array. Kein Text. Direkt mit [ beginnen, mit ] enden.
[{{"day":"Montag","time":"07:45","class":"{cls["name"]}","subject":"Mathematik","teacher":"Fr. Müller"}}]"""

    response = await client.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": "claude-sonnet-4-6", "max_tokens": 4096, "messages": [{"role": "user", "content": prompt}]}
    )

    if response.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Anthropic Fehler für Klasse {cls['name']}: {response.text}")

    data = response.json()
    text = "".join(b.get("text","") for b in data.get("content",[]))
    clean = text.replace("```json","").replace("```","").strip()

    try:
        return json.loads(clean)
    except:
        match = re.search(r'\[[\s\S]*\]', clean)
        if match:
            return json.loads(match.group())
        raise HTTPException(status_code=500, detail=f"JSON-Parse fehlgeschlagen für {cls['name']}: {clean[:200]}")

@app.post("/api/generate")
async def generate_schedule(req: GenerateRequest):
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="API Key nicht konfiguriert")

    all_entries = list(req.locked_entries)

    async with httpx.AsyncClient(timeout=120) as client:
        for cls in req.classes:
            new_entries = await generate_for_class(client, cls, req.teachers, all_entries, req.locked_entries)
            all_entries.extend(new_entries)

    return {"schedule": all_entries, "count": len(all_entries)}
