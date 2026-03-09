from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from collections import defaultdict
import os, json, re

app = FastAPI(title="StundenPlaner API")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
DAYS  = ["Montag","Dienstag","Mittwoch","Donnerstag","Freitag"]
HOURS = ["07:45","08:30","09:30","10:15","11:15","12:00","13:00","13:45"]

def cors(data, status=200):
    return JSONResponse(content=data, status_code=status, headers={
        "Access-Control-Allow-Origin":  "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "*",
    })

@app.options("/{path:path}")
async def options(path: str): return cors({})

@app.get("/")
async def health(): return cors({"status": "ok"})


def build_schedule(curriculum, teachers, existing_entries, hours, days,
                   start_hour, max_per_day, cls_name, cls_index):

    start_idx = max(0, start_hour - 1)
    slots_per_day = len(hours) - start_idx

    # ── Teacher info maps ────────────────────────────────────────────────────
    t_subjects  = {t["name"]: set(t.get("subjects", []))  for t in teachers}
    t_deputat   = {t["name"]: int(t.get("deputat", 999))  for t in teachers}

    # Count hours already used by each teacher (from all previously scheduled classes)
    t_used = defaultdict(int)
    occupied = set()  # "teacher|day|time"
    for e in existing_entries:
        tch = e.get("teacher", "")
        if tch and tch != "kann nicht besetzt werden":
            t_used[tch] += 1
            occupied.add(f"{tch}|{e['day']}|{e['time']}")

    # ── Assign exactly one teacher per subject ───────────────────────────────
    subject_teacher = {}
    for subject, count in curriculum.items():
        need = int(count)
        # Eligible: teaches subject AND has enough remaining deputat
        eligible = [
            t["name"] for t in teachers
            if subject in t_subjects.get(t["name"], set())
            and t_deputat[t["name"]] - t_used[t["name"]] >= need
        ]
        # Fallback: any teacher for subject regardless of remaining capacity
        if not eligible:
            eligible = [t["name"] for t in teachers if subject in t_subjects.get(t["name"], set())]
        # Pick least-loaded
        eligible.sort(key=lambda n: t_used[n])
        assigned = eligible[0] if eligible else "kann nicht besetzt werden"
        subject_teacher[subject] = assigned
        if assigned != "kann nicht besetzt werden":
            t_used[assigned] += need   # reserve these hours

    # ── Build blocks ─────────────────────────────────────────────────────────
    blocks = []
    for subject, total in curriculum.items():
        teacher = subject_teacher[subject]
        count   = int(total)
        max_blk = min(2, max_per_day.get(subject, 2))
        while count > 0:
            b = max_blk if count >= max_blk else 1
            blocks.append([(subject, teacher)] * b)
            count -= b

    blocks.sort(key=lambda b: -len(b))  # largest first

    # ── Rotate start day per class to prevent clustering ─────────────────────
    offset       = cls_index % len(days)
    rotated_days = days[offset:] + days[:offset]
    day_slots    = {d: [] for d in days}  # list of (subject, teacher)

    # ── Place blocks ─────────────────────────────────────────────────────────
    for block in blocks:
        subject = block[0][0]
        teacher = block[0][1]
        size    = len(block)

        def day_score(d):
            already_has_subject = any(s == subject for s, _ in day_slots[d])
            teacher_already_today = any(tch == teacher for _, tch in day_slots[d])
            return (len(day_slots[d])
                    + (50 if already_has_subject else 0)
                    + (15 if teacher_already_today and teacher != "kann nicht besetzt werden" else 0))

        placed = False
        for day in sorted(rotated_days, key=day_score):
            cur = len(day_slots[day])
            if cur + size > slots_per_day:
                continue
            # Check no slot collision for teacher
            ok = True
            for i in range(size):
                slot = hours[start_idx + cur + i]
                if teacher != "kann nicht besetzt werden" and f"{teacher}|{day}|{slot}" in occupied:
                    ok = False
                    break
            if not ok:
                continue
            # ── Hard deputat cap ─────────────────────────────────────────────
            if teacher != "kann nicht besetzt werden":
                already_placed = sum(1 for k in occupied if k.startswith(f"{teacher}|"))
                cap = t_deputat.get(teacher, 999)
                # How many can we still place?
                remaining_cap = cap - already_placed
                if remaining_cap <= 0:
                    teacher = "kann nicht besetzt werden"
                elif remaining_cap < size:
                    # Place as many as allowed, rest unbesetzt
                    pass  # handled slot-by-slot below
            # Place block slot by slot
            for i, (subj, _) in enumerate(block):
                slot = hours[start_idx + cur + i]
                tch = teacher
                if tch != "kann nicht besetzt werden":
                    already = sum(1 for k in occupied if k.startswith(f"{tch}|"))
                    if already >= t_deputat.get(tch, 999):
                        tch = "kann nicht besetzt werden"
                    else:
                        occupied.add(f"{tch}|{day}|{slot}")
                day_slots[day].append((subj, tch))
            placed = True
            break

        if not placed:
            best = min(rotated_days, key=lambda d: len(day_slots[d]))
            for subj, _ in block:
                cur = len(day_slots[best])
                if start_idx + cur < len(hours):
                    day_slots[best].append((subj, "kann nicht besetzt werden"))

    # ── Convert to entries ───────────────────────────────────────────────────
    entries = []
    for day in days:
        for i, (subj, tch) in enumerate(day_slots[day]):
            entries.append({
                "day": day,
                "time": hours[start_idx + i],
                "subject": subj,
                "teacher": tch,
                "class": cls_name
            })
    return entries


@app.post("/api/generate-class")
async def generate_class(request: Request):
    if not ANTHROPIC_API_KEY:
        return cors({"detail": "API Key nicht konfiguriert"}, 500)
    try:
        req = await request.json()
    except:
        return cors({"detail": "Ungültige JSON-Anfrage"}, 400)

    cls         = req.get("cls", {})
    teachers    = req.get("teachers", [])
    existing    = req.get("existing_schedule", [])
    locked      = req.get("locked_entries", [])
    hours       = req.get("hours", HOURS)
    max_per_day = req.get("max_per_day", {})
    start_hour  = req.get("start_hour", 1)
    cls_index   = req.get("cls_index", 0)

    cls_name   = cls.get("name", "?")
    curriculum = {k: v for k, v in (cls.get("curriculum") or {}).items() if v > 0}

    # Filter teachers by grade
    raw = cls_name
    cls_grade = int(''.join(filter(str.isdigit, raw))[:2] or 0) if any(c.isdigit() for c in raw) else 0
    filtered = [t for t in teachers
                if not t.get("grades") or not cls_grade or cls_grade in t.get("grades", [])]

    # Subtract locked entries from curriculum
    locked_for_class = [e for e in locked if e.get("class") == cls_name]
    locked_count = defaultdict(int)
    for e in locked_for_class:
        locked_count[e.get("subject", "")] += 1
    remaining = {s: c - locked_count.get(s, 0) for s, c in curriculum.items()
                 if c - locked_count.get(s, 0) > 0}

    entries = build_schedule(remaining, filtered, existing, hours, DAYS,
                             start_hour, max_per_day, cls_name, cls_index)

    return cors({"entries": locked_for_class + entries, "count": len(locked_for_class) + len(entries)})
