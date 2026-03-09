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


def assign_teachers(curriculum, filtered_teachers, existing_schedule, hours):
    """
    Pre-assign exactly ONE teacher per subject for this class.
    Returns dict: subject -> teacher_name (or "kann nicht besetzt werden")
    Also returns set of already occupied teacher|day|time slots.
    """
    occupied = set()
    for e in existing_schedule:
        t = e.get("teacher","")
        if t and t != "kann nicht besetzt werden":
            occupied.add(f"{t}|{e.get('day','')}|{e.get('time','')}")

    # Build teacher -> subjects map
    teacher_subjects = {t["name"]: set(t.get("subjects",[])) for t in filtered_teachers}

    # Count how many slots each teacher already has occupied
    teacher_load = defaultdict(int)
    for key in occupied:
        name = key.split("|")[0]
        teacher_load[name] += 1

    assignments = {}
    for subject, hours_needed in curriculum.items():
        # Find teachers who can teach this subject, sorted by current load (least busy first)
        candidates = [
            t["name"] for t in filtered_teachers
            if subject in teacher_subjects.get(t["name"], set())
        ]
        candidates.sort(key=lambda n: teacher_load[n])
        assignments[subject] = candidates[0] if candidates else "kann nicht besetzt werden"

    return assignments, occupied


def build_schedule(curriculum, teacher_assignments, occupied, hours, days, start_hour, max_per_day):
    """
    Deterministically build a valid schedule:
    - Exact curriculum counts
    - No teacher collisions
    - No gaps (compact from start_hour)
    - Block pairing for even numbers; odd = pairs + 1 single
    """
    start_idx = max(0, start_hour - 1)
    
    # Plan distribution: subject -> list of (day, count) meaning X consecutive slots on that day
    # Strategy: spread evenly, pair where possible
    subject_plan = []  # list of (subject, teacher, count_on_day) to schedule as blocks
    
    for subject, total in curriculum.items():
        teacher = teacher_assignments[subject]
        max_day = max_per_day.get(subject, 2)
        
        remaining = int(total)  # handle 0.5 for biweekly
        blocks = []
        
        if remaining == 1:
            blocks = [1]
        elif remaining == 2:
            blocks = [2]
        elif remaining == 3:
            blocks = [2, 1]
        elif remaining == 4:
            blocks = [2, 2]
        elif remaining == 5:
            blocks = [2, 2, 1]
        elif remaining == 6:
            blocks = [2, 2, 2]
        else:
            # General: fill with max_day blocks
            while remaining > 0:
                b = min(max_day, remaining)
                blocks.append(b)
                remaining -= b
        
        for b in blocks:
            subject_plan.append((subject, teacher, b))

    # Sort: larger blocks first for better packing
    subject_plan.sort(key=lambda x: -x[2])

    # Assign to days: track slots used per day
    # day_slots[day] = list of entries already assigned
    day_slots = {d: [] for d in days}
    
    entries = []
    
    for subject, teacher, block_size in subject_plan:
        placed = False
        # Try each day
        for day in days:
            current_count = len(day_slots[day])
            # Check if block fits
            if start_idx + current_count + block_size > len(hours):
                continue
            # Check teacher collision for all slots in block
            ok = True
            for i in range(block_size):
                slot_time = hours[start_idx + current_count + i]
                if f"{teacher}|{day}|{slot_time}" in occupied:
                    ok = False
                    break
            if ok:
                # Place block
                for i in range(block_size):
                    slot_time = hours[start_idx + current_count + i]
                    entry = {
                        "day": day,
                        "time": slot_time,
                        "subject": subject,
                        "teacher": teacher,
                        "class": ""  # filled in caller
                    }
                    day_slots[day].append(entry)
                    entries.append(entry)
                    if teacher != "kann nicht besetzt werden":
                        occupied.add(f"{teacher}|{day}|{slot_time}")
                placed = True
                break
        
        if not placed:
            # Force-place with "kann nicht besetzt werden" on least-loaded day
            best_day = min(days, key=lambda d: len(day_slots[d]))
            for i in range(block_size):
                current_count = len(day_slots[best_day])
                if start_idx + current_count >= len(hours):
                    break
                slot_time = hours[start_idx + current_count]
                entry = {
                    "day": best_day,
                    "time": slot_time,
                    "subject": subject,
                    "teacher": "kann nicht besetzt werden",
                    "class": ""
                }
                day_slots[best_day].append(entry)
                entries.append(entry)

    return entries


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
    start_hour      = req.get("start_hour", 1)

    cls_name = cls.get("name","?")
    curriculum = {k: v for k, v in (cls.get("curriculum") or {}).items() if v > 0}

    # Filter teachers by grade
    cls_name_str = cls.get("name","")
    cls_grade = int(''.join(filter(str.isdigit, cls_name_str))[:2] or 0) if any(c.isdigit() for c in cls_name_str) else 0
    filtered_teachers = [t for t in teachers if not t.get("grades") or not cls_grade or cls_grade in t.get("grades",[])]

    # Handle locked entries - add them first
    locked_for_class = [e for e in locked if e.get("class") == cls_name]
    locked_subjects = defaultdict(int)
    for e in locked_for_class:
        locked_subjects[e.get("subject","")] += 1

    # Reduce curriculum by already-locked entries
    remaining_curriculum = {}
    for subj, count in curriculum.items():
        remaining = count - locked_subjects.get(subj, 0)
        if remaining > 0:
            remaining_curriculum[subj] = remaining

    # Pre-assign teachers
    teacher_assignments, occupied = assign_teachers(remaining_curriculum, filtered_teachers, existing, hours)

    # Build schedule deterministically
    entries = build_schedule(remaining_curriculum, teacher_assignments, occupied, hours, DAYS, start_hour, max_per_day)

    # Set class name on all entries
    for e in entries:
        e["class"] = cls_name

    # Merge with locked entries
    all_entries = locked_for_class + entries

    return cors({"entries": all_entries, "count": len(all_entries)})
