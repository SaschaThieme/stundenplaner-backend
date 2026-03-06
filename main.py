from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx, os, json, re

app = FastAPI(title="StundenPlaner API")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
DAYS  = ["Montag","Dienstag","Mittwoch","Donnerstag","Freitag"]
HOURS = ["07:45","08:30","09:30","10:15","11:15","12:00","13:00","13:45"]

def cors(data, status=200):
    return JSONResponse(content=data, status_code=status, headers={
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "*",
    })

@app.options("/{path:path}")
async def options(path: str):
    return cors({})

@app.get("/")
async def health():
    return cors({"status": "ok", "service": "StundenPlaner API"})

@app.post("/api/generate-class")
async def generate_class(request: Request):
    if not ANTHROPIC_API_KEY:
        return cors({"detail": "API Key nicht konfiguriert"}, 500)
    try:
        req = await request.json()
    except:
        return cors({"detail": "Ungültige JSON-Anfrage"}, 400)

    cls      = req.get("cls", {})
    teachers = req.get("teachers", [])
    existing = req.get("existing_schedule", [])
    locked   = req.get("locked_entries", [])

    occupied = [f"{e['teacher']}|{e['day']}|{e['time']}" for e in existing]
    occ_info = f"\nBEREITS BELEGTE LEHRER-SLOTS (nicht verwenden):\n{json.dumps(occupied, ensure_ascii=False)}" if occupied else ""
    lck      = [e for e in locked if e.get("class") == cls.get("name")]
    lck_info = f"\nGESPERRTE EINTRÄGE: {json.dumps(lck, ensure_ascii=False)}" if lck else ""

    prompt = f"""Du bist ein Stundenplan-Solver für eine deutsche Schule (Sek I).
Erstelle den Stundenplan NUR für Klasse {cls.get("name","?")}.

KLASSE: {json.dumps(cls, ensure_ascii=False)}
LEHRKRÄFTE: {json.dumps([{{"name":t["name"],"subjects":t["subjects"]}} for t in teachers], ensure_ascii=False)}
TAGE: {json.dumps(DAYS, ensure_ascii=False)}
ZEITSLOTS: {json.dumps(HOURS, ensure_ascii=False)}{occ_info}{lck_info}

Regeln:
1. Lehrkraft nie doppelt im selben Tag/Zeit-Slot
2. Bereits belegte Slots NICHT verwenden
3. Lehrkraft unterrichtet nur ihre Fächer
4. Stundenzahlen exakt wie im curriculum
5. Kernfächer (Mathe,Deutsch) möglichst in Stunden 1-5
6. Gleichmäßige Verteilung über die Woche

AUSGABE: Nur rohes JSON-Array. Kein Text. Direkt mit [ beginnen, mit ] enden.
[{{"day":"Montag","time":"07:45","class":"{cls.get("name","?")}","subject":"Mathematik","teacher":"Fr. Müller"}}]"""

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": "claude-sonnet-4-6", "max_tokens": 4096, "messages": [{"role": "user", "content": prompt}]}
            )
    except Exception as e:
        return cors({"detail": f"Netzwerkfehler: {str(e)}"}, 502)

    if response.status_code != 200:
        return cors({"detail": f"Anthropic Fehler: {response.text[:200]}"}, 502)

    data  = response.json()
    text  = "".join(b.get("text","") for b in data.get("content",[]))
    clean = text.replace("```json","").replace("```","").strip()

    try:
        entries = json.loads(clean)
    except:
        match = re.search(r'\[[\s\S]*\]', clean)
        if match:
            try:
                entries = json.loads(match.group())
            except:
                return cors({"detail": f"JSON-Parse fehlgeschlagen: {clean[:200]}"}, 500)
        else:
            return cors({"detail": f"Kein JSON in Antwort: {clean[:200]}"}, 500)

    return cors({"entries": entries, "count": len(entries)})
