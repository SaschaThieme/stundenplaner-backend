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
async def options(path: str): return cors({})

@app.get("/")
async def health(): return cors({"status": "ok", "service": "StundenPlaner API"})


def build_schedule(curriculum, filtered_teachers, existing_occupied, hours, days, start_hour, max_per_day, cls_name, cls_index=0):
    """
    Fully deterministic schedule builder.
    - One teacher per subject (pre-assigned)
    - Exact curriculum counts
    - Blocks of 2 spread across days (never all on one day)
    - No gaps within a day's lessons
    - No teacher collisions
    """
    start_idx = max(0, start_hour - 1)
    available_per_day = len(hours) - start_idx

    # occupied: set of "teacher|day|time"
    occupied = set(existing_occupied)

    # Pre-assign one teacher per subject
    teacher_subjects = {t["name"]: set(t.get("subjects", [])) for t in filtered_teachers}
    teacher_load = defaultdict(int)
    for key in occupied:
        teacher_load[key.split("|")[0]] += 1

    subject_teacher = {}
    for subject in curriculum:
        candidates = sorted(
            [t["name"] for t in filtered_teachers if subject in teacher_subjects.get(t["name"], set())],
            key=lambda n: teacher_load[n]
        )
        subject_teacher[subject] = candidates[0] if candidates else "kann nicht besetzt werden"

    # Build list of "lesson tokens" to place: each token = one lesson
    # Group into blocks: prefer pairs (2), singles only if odd remainder
    tokens = []  # list of (subject, teacher, block_id) — same block_id = must be consecutive
    block_id = 0
    for subject, total in curriculum.items():
        teacher = subject_teacher[subject]
        count = int(total)
        max_d = max_per_day.get(subject, 2)
        max_block = min(2, max_d)  # never more than 2 in a block unless max_per_day forces it

        while count > 0:
            b = max_block if count >= max_block else 1
            for _ in range(b):
                tokens.append((subject, teacher, block_id))
            block_id += 1
            count -= b

    # day_schedule[day] = list of (subject, teacher) in slot order from start_idx
    day_schedule = {d: [] for d in days}

    # Place blocks: iterate through blocks in round-robin across days
    # Group tokens by block_id
    blocks = []
    current_block = []
    current_id = None
    for subj, teacher, bid in tokens:
        if bid != current_id:
            if current_block:
                blocks.append(current_block)
            current_block = [(subj, teacher)]
            current_id = bid
        else:
            current_block.append((subj, teacher))
    if current_block:
        blocks.append(current_block)

    # Sort blocks largest first for better packing
    blocks.sort(key=lambda b: -len(b))

    # Rotate preferred starting day per class to prevent clustering
    offset = cls_index % len(days)
    rotated_days = days[offset:] + days[:offset]
    # Round-robin day assignment: try each day starting from least-loaded
    for block in blocks:
        block_size = len(block)
        teacher = block[0][1]  # all same teacher in block

        # Try days in order of current load (least loaded first)
        # Prefer days with fewest total slots AND where teacher doesn't already appear
        teacher_on_day = set(
            entry_day for entry_day, entry_list in day_schedule.items()
            for _, tch in entry_list if tch == teacher
        )
        def day_score(d):
            slots_used = len(day_schedule[d])
            teacher_penalty = 20 if (d in teacher_on_day and teacher != "kann nicht besetzt werden") else 0
            return slots_used + teacher_penalty
        day_order = sorted(rotated_days, key=day_score)
        placed = False

        for day in day_order:
            cur = len(day_schedule[day])
            if cur + block_size > available_per_day:
                continue
            # Check teacher not already on this day at these slots
            ok = True
            for i in range(block_size):
                slot = hours[start_idx + cur + i]
                if teacher != "kann nicht besetzt werden" and f"{teacher}|{day}|{slot}" in occupied:
                    ok = False
                    break
            if ok:
                for i, (subj, tch) in enumerate(block):
                    slot = hours[start_idx + cur + i]
                    day_schedule[day].append((subj, tch))
                    if tch != "kann nicht besetzt werden":
                        occupied.add(f"{tch}|{day}|{slot}")
                placed = True
                break

        if not placed:
            # Fallback: force onto least-loaded day ignoring teacher collision → mark unbesetzt
            day = min(rotated_days, key=lambda d: len(day_schedule[d]))
            for subj, _ in block:
                cur = len(day_schedule[day])
                if start_idx + cur < len(hours):
                    day_schedule[day].append((subj, "kann nicht besetzt werden"))

    # Convert to entry list
    entries = []
    for day in days:
        for i, (subj, teacher) in enumerate(day_schedule[day]):
            entries.append({
                "day": day,
                "time": hours[start_idx + i],
                "subject": subj,
                "teacher": teacher,
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

    cls_name    = cls.get("name", "?")
    cls_index   = req.get("cls_index", 0)   # which class in generation order (0,1,2,...)
    curriculum  = {k: v for k, v in (cls.get("curriculum") or {}).items() if v > 0}

    # Filter teachers by grade
    cls_name_str = cls.get("name", "")
    cls_grade = int(''.join(filter(str.isdigit, cls_name_str))[:2] or 0) if any(c.isdigit() for c in cls_name_str) else 0
    filtered_teachers = [
        t for t in teachers
        if not t.get("grades") or not cls_grade or cls_grade in t.get("grades", [])
    ]

    # Handle locked entries
    locked_for_class = [e for e in locked if e.get("class") == cls_name]
    locked_subjects = defaultdict(int)
    for e in locked_for_class:
        locked_subjects[e.get("subject", "")] += 1

    # Subtract locked from curriculum
    remaining = {s: c - locked_subjects.get(s, 0) for s, c in curriculum.items() if c - locked_subjects.get(s, 0) > 0}

    # Build occupied set from existing schedule
    existing_occupied = [
        f"{e.get('teacher','')}|{e.get('day','')}|{e.get('time','')}"
        for e in existing
        if e.get("teacher", "") and e.get("teacher", "") != "kann nicht besetzt werden"
    ]

    entries = build_schedule(remaining, filtered_teachers, existing_occupied, hours, DAYS, start_hour, max_per_day, cls_name, cls_index)
    all_entries = locked_for_class + entries

    return cors({"entries": all_entries, "count": len(all_entries)})
