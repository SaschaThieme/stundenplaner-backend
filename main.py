from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx, os, json, re
from typing import Optional

app = FastAPI(title="StundenPlaner API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"])

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
DAYS  = ["Montag","Dienstag","Mittwoch","Donnerstag","Freitag"]
HOURS = ["07:45","08:30","09:30","10:15","11:15","12:00","13:00","13:45"]

class GenerateRequest(BaseModel):
    teachers: list
    classes: list
    locked_entries: list = []

class GenerateClassRequest(BaseModel):
    cls: dict
    teachers: list
    existing_schedule: list = []
    locked_entries: list = []

@app.get("/")
def health():
    return {"status": "ok", "service": "StundenPlaner API"}

@app.post("/api/generate-class")
async def generate_class(req: GenerateClassRequest):
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="API Key nicht konfiguriert")

    occupied = [f"{e['teacher']}|{e['day']}|{e['time']}" for e in req.existing_schedule]
    occupied_info = f"\nBEREITS BELEGTE LEHRER-SLOTS (nicht verwenden):\n{json.dumps(occupied, ensure_ascii=False)}" if occupied else ""
    locked = [e for e in req.locked_entries if e.get("class") == req.cls.get("name")]
    locked_info = f"\nGESPERRTE EINTRÄGE: {json.dumps(locked, ensure_ascii=False)}" if locked else ""

    prompt = f"""Du bist ein Stundenplan-Solver für eine deutsche Schule (Sek I).
Erstelle den Stundenplan NUR für Klasse {req.cls["name"]}.

KLASSE: {json.dumps(req.cls, ensure_ascii=False)}
LEHRKRÄFTE: {json.dumps([{{"name":t["name"],"subjects":t["subjects"]}} for t in req.teachers], ensure_ascii=False)}
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
[{{"day":"Montag","time":"07:45","class":"{req.cls["name"]}","subject":"Mathematik","teacher":"Fr. Müller"}}]"""

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-sonnet-4-6", "max_tokens": 4096, "messages": [{"role": "user", "content": prompt}]}
        )

    if response.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Anthropic Fehler: {response.text[:200]}")

    data = response.json()
    text = "".join(b.get("text","") for b in data.get("content",[]))
    clean = text.replace("```json","").replace("```","").strip()

    try:
        entries = json.loads(clean)
    except:
        match = re.search(r'\[[\s\S]*\]', clean)
        if match:
            entries = json.loads(match.group())
        else:
            raise HTTPException(status_code=500, detail=f"JSON-Parse fehlgeschlagen: {clean[:200]}")

    return {"entries": entries, "count": len(entries)}

# Legacy endpoint
@app.post("/api/generate")
async def generate_schedule(req: GenerateRequest):
    return {"error": "Bitte /api/generate-class verwenden"}, 400
