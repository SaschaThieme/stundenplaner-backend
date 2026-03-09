from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx, os, json, re
from collections import defaultdict

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

def post_process(entries, existing_schedule, teachers, cls_name):
    """
    Fix teacher collisions AFTER Claude generates the plan:
    - If a teacher is already used in existing_schedule at that day/time → replace with 'kann nicht besetzt werden'
    - Build a local collision set within this class too
    """
    # Build set of already occupied slots from previous classes
    occupied = set()
    for e in existing_schedule:
        t = e.get("teacher","")
        if t and t != "kann nicht besetzt werden":
            occupied.add(f"{t}|{e.get('day','')}|{e.get('time','')}")

    # Build teacher→subjects map for validation
    teacher_subjects = {}
    for t in teachers:
        teacher_subjects[t.get("name","")] = set(t.get("subjects",[]))

    fixed = []
    local_occupied = set()  # collisions within this class's new entries

    for e in entries:
        teacher = e.get("teacher","")
        day = e.get("day","")
        time = e.get("time","")
        subject = e.get("subject","")
        slot_key = f"{teacher}|{day}|{time}"

        # Fix: teacher already used elsewhere at same time
        if teacher and teacher != "kann nicht besetzt werden":
            if slot_key in occupied or slot_key in local_occupied:
                e = {**e, "teacher": "kann nicht besetzt werden"}
            # Fix: teacher doesn't teach this subject
            elif teacher in teacher_subjects and subject not in teacher_subjects[teacher]:
                e = {**e, "teacher": "kann nicht besetzt werden"}
            else:
                local_occupied.add(slot_key)
                occupied.add(slot_key)

        fixed.append(e)

    return fixed

@app.post("/api/generate-class")
async def generate_class(request: Request):
    if not ANTHROPIC_API_KEY:
        return cors({"detail": "API Key nicht konfiguriert"}, 500)
    try:
        req = await request.json()
    except:
        return cors({"detail": "Ungültige JSON-Anfrage"}, 400)

    cls             = req.get("cls", {})
    teachers        = req.get("teachers", [])
    existing        = req.get("existing_schedule", [])
    locked          = req.get("locked_entries", [])
    hours           = req.get("hours", HOURS)
    max_per_day     = req.get("max_per_day", {})
    no_free_periods = req.get("no_free_periods", True)
    start_hour      = req.get("start_hour", 1)

    # Build occupied slots string for prompt
    occupied_list = [
        f"{e.get('teacher','')}|{e.get('day','')}|{e.get('time','')}"
        for e in existing
        if e.get("teacher","") and e.get("teacher","") != "kann nicht besetzt werden"
    ]
    occ_info = ("\nBEREITS BELEGTE LEHRER-SLOTS (STRIKT verboten für diese Klasse):\n"
                + json.dumps(occupied_list, ensure_ascii=False)) if occupied_list else ""

    lck = [e for e in locked if e.get("class") == cls.get("name")]
    lck_info = ("\nGESPERRTE EINTRÄGE: " + json.dumps(lck, ensure_ascii=False)) if lck else ""

    # Filter teachers by grade
    cls_name_str = cls.get("name","")
    cls_grade = int(''.join(filter(str.isdigit, cls_name_str))[:2] or 0) if any(c.isdigit() for c in cls_name_str) else 0
    filtered_teachers = [t for t in teachers if not t.get("grades") or not cls_grade or cls_grade in t.get("grades",[])]
    teacher_list = [{"name": t.get("name",""), "subjects": t.get("subjects",[])} for t in filtered_teachers]

    cls_name = cls.get("name","?")
    curriculum = cls.get("curriculum", {})

    # Which subjects have no teacher available?
    covered = set(s for t in filtered_teachers for s in t.get("subjects",[]))
    unplannable = [s for s in curriculum if s not in covered]

    start_rule = ""
    if start_hour > 1 and len(hours) >= start_hour:
        start_rule = (f"9. UNTERRICHTSBEGINN: Klasse beginnt erst ab Zeitslot Nr.{start_hour} "
                      f"(Zeit {hours[start_hour-1]}). Slots davor bleiben LEER.\n")

    max_rule = ""
    if max_per_day:
        max_rule = f"10. MAX PRO TAG: {json.dumps(max_per_day, ensure_ascii=False)}\n"

    prompt = (
        f"Du bist ein Stundenplan-Solver fuer Klasse {cls_name} an einer deutschen Schule.\n\n"
        f"KLASSE: {json.dumps(cls, ensure_ascii=False)}\n"
        f"LEHRKRAEFTE (nur diese duerfen verwendet werden): {json.dumps(teacher_list, ensure_ascii=False)}\n"
        f"TAGE: {json.dumps(DAYS, ensure_ascii=False)}\n"
        f"ZEITSLOTS: {json.dumps(hours, ensure_ascii=False)}\n"
        + occ_info + lck_info + "\n\n"
        "STRIKTE REGELN:\n"
        "1. KOLLISION VERBOTEN: Jede Lehrkraft darf pro Tag+Zeit nur EINMAL eingeplant werden - auch klassenübergreifend!\n"
        "   Die oben genannten belegten Slots sind ABSOLUT verboten.\n"
        "2. Lehrkraft unterrichtet NUR ihre eigenen Faecher (siehe LEHRKRAEFTE-Liste)\n"
        "3. Stundenzahlen EXAKT wie im curriculum\n"
        "4. KEINE FREISTUNDEN innerhalb des Tages einer Klasse\n"
        "5. BLOCKBILDUNG: Mehrere Stunden desselben Fachs an einem Tag → immer direkt nacheinander\n"
        "6. Gleichmaessige Verteilung ueber die Woche\n"
        + start_rule + max_rule +
        "\nWICHTIG: Falls kein Lehrer fuer ein Fach verfuegbar ist (wegen Kollision oder fehlendem Fach), "
        "trage 'kann nicht besetzt werden' als teacher ein. NIEMALS eine Stunde weglassen!\n"
        + (f"\nHINWEIS: Fuer folgende Faecher gibt es KEINEN Lehrer, trage direkt 'kann nicht besetzt werden' ein: {unplannable}\n" if unplannable else "")
        + f'\nAUSGABE: Nur rohes JSON-Array, direkt mit [ beginnen:\n'
        f'[{{"day":"Montag","time":"{hours[0] if hours else "07:45"}","class":"{cls_name}","subject":"Mathematik","teacher":"Fr. Mueller"}}]'
    )

    try:
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 8192,
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

    entries = None
    try:
        entries = json.loads(clean)
    except:
        pass

    if entries is None:
        match = re.search(r'\[[\s\S]*\]', clean)
        if match:
            try: entries = json.loads(match.group())
            except: pass

    if entries is None:
        match = re.search(r'\[[\s\S]*\}', clean)
        if match:
            partial = match.group()
            depth, last_complete = 0, 0
            for i, ch in enumerate(partial):
                if ch == '{': depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0: last_complete = i + 1
            if last_complete > 0:
                try: entries = json.loads(partial[:last_complete] + ']')
                except: pass

    if entries is None:
        return cors({"detail": f"JSON-Parse fehlgeschlagen: {clean[:300]}"}, 500)

    # POST-PROCESS: fix any remaining collisions deterministically
    entries = post_process(entries, existing, filtered_teachers, cls_name)

    return cors({"entries": entries, "count": len(entries)})
