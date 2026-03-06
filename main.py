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
    hours         = req.get("hours", HOURS)
    max_per_day      = req.get("max_per_day", {})
    block_subjects   = req.get("block_subjects", True)
    no_free_periods  = req.get("no_free_periods", True)

    # Belegte Lehrer-Slots
    occupied = [str(e.get("teacher","")) + "|" + str(e.get("day","")) + "|" + str(e.get("time","")) for e in existing]
    occ_info = ("\nBEREITS BELEGTE LEHRER-SLOTS (nicht verwenden):\n" + json.dumps(occupied, ensure_ascii=False)) if occupied else ""

    # Gesperrte Einträge für diese Klasse
    lck = [e for e in locked if e.get("class") == cls.get("name")]
    lck_info = ("\nGESPERRTE EINTRÄGE: " + json.dumps(lck, ensure_ascii=False)) if lck else ""

    # Lehrkräfte vereinfachen
    cls_name_str = cls.get("name","")
    cls_grade = int(''.join(filter(str.isdigit, cls_name_str))[:2] or 0) if any(c.isdigit() for c in cls_name_str) else 0
    filtered_teachers = [t for t in teachers if not t.get("grades") or not cls_grade or cls_grade in t.get("grades",[])]
    teacher_list = [{"name": t.get("name",""), "subjects": t.get("subjects",[])} for t in filtered_teachers]

    cls_name = cls.get("name", "?")

    prompt = (
        "Du bist ein Stundenplan-Solver fuer eine deutsche Schule (Sek I).\n"
        "Erstelle den Stundenplan NUR fuer Klasse " + cls_name + ".\n\n"
        "KLASSE: " + json.dumps(cls, ensure_ascii=False) + "\n"
        "LEHRKRAEFTE: " + json.dumps(teacher_list, ensure_ascii=False) + "\n"
        "TAGE: " + json.dumps(DAYS, ensure_ascii=False) + "\n"
        "ZEITSLOTS: " + json.dumps(hours, ensure_ascii=False) + "\n"
        + occ_info + lck_info + "\n\n"
        "Regeln:\n"
        "1. Lehrkraft nie doppelt im selben Tag/Zeit-Slot\n"
        "2. Bereits belegte Slots NICHT verwenden\n"
        "3. Lehrkraft unterrichtet nur ihre Faecher\n"
        "4. Stundenzahlen exakt wie im curriculum\n"
        "5. Kernfaecher (Mathe,Deutsch) moeglichst in Stunden 1-5\n"
        "6. Gleichmaessige Verteilung ueber die Woche\n"
        "7. BLOCKBILDUNG PFLICHT: Mehrere Stunden desselben Fachs an einem Tag muessen direkt aufeinanderfolgend sein\n"
        "8. KEINE FREISTUNDEN: Der Stundenplan einer Klasse darf keine Luecken haben - Stunden muessen zusammenhaengend sein\n"
        + (("8. MAX STUNDEN PRO TAG: " + json.dumps(max_per_day, ensure_ascii=False) + "\n") if max_per_day else "")
        + "\n"
        "AUSGABE: Nur rohes JSON-Array. Kein Text. Direkt mit [ beginnen, mit ] enden.\n"
        '[{"day":"Montag","time":"07:45","class":"' + cls_name + '","subject":"Mathematik","teacher":"Fr. Mueller"}]'
    )

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 4096,
                    "messages": [{"role": "user", "content": prompt}]
                }
            )
    except Exception as e:
        return cors({"detail": "Netzwerkfehler: " + str(e)}, 502)

    if response.status_code != 200:
        return cors({"detail": "Anthropic Fehler: " + response.text[:200]}, 502)

    data  = response.json()
    text  = "".join(b.get("text","") for b in data.get("content",[]))
    clean = text.replace("```json","").replace("```","").strip()

    try:
        entries = json.loads(clean)
        return cors({"entries": entries, "count": len(entries)})
    except:
        match = re.search(r'\[[\s\S]*\]', clean)
        if match:
            try:
                entries = json.loads(match.group())
                return cors({"entries": entries, "count": len(entries)})
            except:
                pass
        return cors({"detail": "JSON-Parse fehlgeschlagen: " + clean[:200]}, 500)
