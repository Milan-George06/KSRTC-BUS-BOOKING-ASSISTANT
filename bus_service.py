import os
import re
import json
import random
import requests
from datetime import date, timedelta, datetime
from groq import Groq

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
CITIES_FILE = os.path.join(os.path.dirname(__file__), "getCities.json")

_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

with open(CITIES_FILE, "r") as fp:
    CITIES = json.load(fp)


# ---------------------------------------------------------------------------
# SMALL TALK
# ---------------------------------------------------------------------------
_GREETING_WORDS = {
    "hi", "hii", "hiii", "hello", "hey", "heya", "yo", "sup", "whatsup",
    "good morning", "good afternoon", "good evening", "namaste",
}
_HOWAREYOU_WORDS = {"how are you", "how r u", "hows it going", "how are u"}
_THANKS_WORDS = {"thanks", "thank you", "thankyou", "thnx", "tnx", "ty"}
_BYE_WORDS = {"bye", "goodbye", "see you", "cya", "ok bye", "gtg"}


def classify_small_talk(text: str) -> str | None:
    """Returns 'greeting' | 'howareyou' | 'thanks' | 'bye' | None."""
    t = re.sub(r"[^a-z0-9 ]", "", text.strip().lower())
    if not t:
        return None
    if any(t == w or t.startswith(w + " ") or t == w for w in _GREETING_WORDS):
        return "greeting"
    if any(w in t for w in _HOWAREYOU_WORDS):
        return "howareyou"
    if any(w in t for w in _THANKS_WORDS):
        return "thanks"
    if any(w in t for w in _BYE_WORDS):
        return "bye"
    return None


def small_talk_reply(kind: str, username: str = "") -> str:
    name = f", {username}" if username else ""
    replies = {
        "greeting": f"Hey{name}! Where would you like to travel? "
                    "(e.g. 'from Kottayam to Ernakulam on 2026-06-17, 2 seats')",
        "howareyou": "Doing great, ready to find you a bus! Where would you like to go?",
        "thanks": "You're welcome! Anything else — another route, more seats?",
        "bye": "Bye! Come back anytime you need to book a KSRTC bus.",
    }
    return replies.get(kind, "Hi! How can I help you book a bus today?")


# ---------------------------------------------------------------------------
# RELATIVE DATE RESOLUTION (deterministic, done before the LLM call so it
# never has to do date arithmetic itself)
# ---------------------------------------------------------------------------
_AFTER_N_DAYS_RE = re.compile(r"after (\d+) days?", re.IGNORECASE)
_IN_N_DAYS_RE = re.compile(r"\bin (\d+) days?\b", re.IGNORECASE)
_DAY_AFTER_TOMORROW_RE = re.compile(r"day after tomorrow", re.IGNORECASE)
_TOMORROW_RE = re.compile(r"tomorrow", re.IGNORECASE)
_TODAY_RE = re.compile(r"\btoday\b", re.IGNORECASE)


def resolve_relative_date(text: str) -> tuple[str, str | None]:
    """Finds relative-date phrases ('tomorrow', 'day after tomorrow',
    'after 3 days', 'in 3 days') and replaces them in `text` with an
    explicit ISO date, computed in Python (not the LLM).
    Returns (rewritten_text, resolved_date_or_None).
    """
    for pattern in (_AFTER_N_DAYS_RE, _IN_N_DAYS_RE):
        m = pattern.search(text)
        if m:
            n = int(m.group(1))
            resolved = (date.today() + timedelta(days=n)).isoformat()
            return pattern.sub(resolved, text, count=1), resolved

    if _DAY_AFTER_TOMORROW_RE.search(text):
        resolved = (date.today() + timedelta(days=2)).isoformat()
        return _DAY_AFTER_TOMORROW_RE.sub(resolved, text, count=1), resolved

    if _TOMORROW_RE.search(text):
        resolved = (date.today() + timedelta(days=1)).isoformat()
        return _TOMORROW_RE.sub(resolved, text, count=1), resolved

    if _TODAY_RE.search(text):
        resolved = date.today().isoformat()
        return _TODAY_RE.sub(resolved, text, count=1), resolved

    return text, None


def is_past_date(date_str: str) -> bool:
    """True if date_str (YYYY-MM-DD) is strictly before today. Malformed
    strings are treated as 'not past' so they fall through to the normal
    slot-filling flow instead of silently failing here."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return False
    return d < date.today()


# ---------------------------------------------------------------------------
# TIME OF DAY: extraction from free text + bucketing of HH:MM:SS times
# ---------------------------------------------------------------------------
_TIME_OF_DAY_WORDS = {
    "morning": ("morning",),
    "afternoon": ("afternoon", "noon"),
    "evening": ("evening",),
    "night": ("night", "tonight"),
}


def extract_time_of_day(text: str) -> str | None:
    """Scans free text for a time-of-day word and returns one of
    'morning' | 'afternoon' | 'evening' | 'night', or None."""
    t = text.lower()
    for bucket, words in _TIME_OF_DAY_WORDS.items():
        if any(re.search(rf"\b{re.escape(w)}\b", t) for w in words):
            return bucket
    return None


def time_of_day_bucket(time_str: str) -> str | None:
    """Classifies a 'HH:MM:SS' (24h) departure time into a bucket:
    morning   01:00–12:00
    afternoon 12:01–15:00
    evening   15:01–18:30
    night     18:31–00:59
    """
    try:
        parts = time_str.split(":")
        h, m = int(parts[0]), int(parts[1])
    except (ValueError, AttributeError, IndexError):
        return None

    minutes = h * 60 + m
    if 60 <= minutes <= 12 * 60:
        return "morning"
    if 12 * 60 + 1 <= minutes <= 15 * 60:
        return "afternoon"
    if 15 * 60 + 1 <= minutes <= 18 * 60 + 30:
        return "evening"
    return "night"  # 18:31–23:59 and 00:00–00:59


# ---------------------------------------------------------------------------
# QUERY PARSING / SLOT FILLING
# ---------------------------------------------------------------------------
REQUIRED_SLOTS = ("from_city", "to_city", "date")

_SLOT_PROMPTS = {
    "from_city": "Which city are you traveling **from**?",
    "to_city": "Which city are you traveling **to**?",
    "date": "What **date** are you traveling? (e.g. 2026-06-17)",
}


def parse_query(user_query: str, known: dict | None = None) -> dict:
    """
    Extracts from_city / to_city / date / num_seats from free text.
    `known` = slot values already collected in this conversation, so the model
    only needs to fill in what's missing / newly mentioned.
    Never raises on missing fields - returns None for anything not found.
    Only raises if Groq isn't configured.

    Note: relative date phrases ('tomorrow', 'after 3 days', ...) are
    expected to already be resolved to explicit ISO dates in `user_query`
    by resolve_relative_date() before this is called.
    """
    if _client is None:
        raise RuntimeError("GROQ_API_KEY is not set. Add it to backend/.env")

    known = known or {}
    today_str = date.today().isoformat()

    prompt = f"""
    You are filling slots for a bus booking assistant.
    Already known (may be null): from_city={known.get('from_city')}, to_city={known.get('to_city')},
    date={known.get('date')}, num_seats={known.get('num_seats')}

    New user message: '{user_query}'

    Extract from_city, to_city, date and num_seats mentioned in the NEW message only.
    If a value isn't mentioned in the new message, return null for it - do NOT invent or reuse old values.
    Convert common city aliases:
    TVM/Trivandrum -> Thiruvananthapuram
    EKM/Kochi/Cochin -> Ernakulam
    Calicut/CLT -> Kozhikode
    ktm -> Kottayam
    If the message already contains an explicit date in YYYY-MM-DD format, use it as-is.
    Otherwise resolve relative dates like 'today'/'tomorrow' using today's date {today_str}, format as YYYY-MM-DD.
    num_seats should be a plain integer if the user says how many seats/tickets they want, else null.

    Return ONLY a valid JSON object exactly like:
    {{"from_city": "CityName or null", "to_city": "CityName or null", "date": "YYYY-MM-DD or null", "num_seats": integer or null}}
    """

    completion = _client.chat.completions.create(
        messages=[{"role": "user", "content": prompt}],
        model="openai/gpt-oss-120b",
        response_format={"type": "json_object"},
    )

    try:
        intent = json.loads(completion.choices[0].message.content)
    except (ValueError, AttributeError, IndexError):
        intent = {}

    return {
        "from_city": intent.get("from_city") or None,
        "to_city": intent.get("to_city") or None,
        "date": intent.get("date") or None,
        "num_seats": intent.get("num_seats") or None,
    }


def missing_slots(slots: dict) -> list[str]:
    return [s for s in REQUIRED_SLOTS if not slots.get(s)]


def build_need_info_message(missing: list[str], slots: dict) -> str:
    known_bits = []
    if slots.get("num_seats") and slots["num_seats"] != 1:
        known_bits.append(f"{slots['num_seats']} seat(s)")
    if slots.get("from_city"):
        known_bits.append(f"from {slots['from_city']}")
    if slots.get("to_city"):
        known_bits.append(f"to {slots['to_city']}")
    if slots.get("date"):
        known_bits.append(f"on {slots['date']}")

    prefix = f"Got it — {', '.join(known_bits)}. " if known_bits else ""
    return prefix + _SLOT_PROMPTS.get(missing[0], f"Could you tell me the {missing[0]}?")


# ---------------------------------------------------------------------------
# CITY / ROUTE LOOKUP
# ---------------------------------------------------------------------------
def get_city_id(name: str) -> str | None:
    for city_id, details in CITIES.items():
        if name.lower() in details["Name"].lower():
            return city_id
    return None


def search_buses(from_name: str, to_name: str, date_time: str) -> dict:
    """Returns {'ok': True, 'buses': [...]} or {'ok': False, 'error': str}"""
    from_id = get_city_id(from_name)
    to_id = get_city_id(to_name)

    if not from_id or not to_id:
        return {"ok": False, "error": f"Could not find city IDs for '{from_name}' or '{to_name}'."}

    api_url = (
        "https://assets.onlineksrtcswift.com/api/resource/searchRoutesV4"
        f"?fromCityID={from_id}&toCityID={to_id}&fromCityName={from_name}"
        f"&toCityName={to_name}&journeyDate={date_time}&mode=oneway"
    )
    try:
        response = requests.get(api_url, timeout=15)
    except requests.exceptions.RequestException:
        return {"ok": False, "error": "KSRTC's server didn't respond in time. Please try again."}

    if response.status_code != 200:
        return {"ok": False, "error": f"Error connecting to KSRTC. Status: {response.status_code}"}

    bus_data = response.json()
    return {"ok": True, "buses": bus_data, "from_id": from_id, "to_id": to_id}


def get_seat_arrangement(route_id: str, date_time: str) -> dict:
    """Fetches seat layout and computes available/window/gendered seats."""
    seat_api_url = (
        f"https://onlineksrtcswift.com/api/resource/seatArrangement"
        f"?routeID={route_id}&journeyDate={date_time}"
    )
    try:
        seat_response = requests.get(seat_api_url, timeout=15)
    except requests.exceptions.RequestException:
        return {"ok": False, "error": "KSRTC's server didn't respond in time while fetching seats. Please try again."}

    if seat_response.status_code != 200:
        return {"ok": False, "error": f"Error fetching seat arrangement. Status: {seat_response.status_code}"}

    raw_data = seat_response.json()
    result = raw_data.get("APIGetChartMicrositeResult", {})

    if not result.get("Status"):
        return {"ok": False, "error": result.get("ErrorMessage", "Unknown error fetching seats.")}

    seats = result.get("Seats") or []

    # Window seats: leftmost/rightmost column per row
    rows: dict[int, list] = {}
    for seat in seats:
        if not seat.get("IsAisle") and seat.get("SeatLabel"):
            rows.setdefault(seat["RowNo"], []).append(seat)

    window_seats = []
    for row_seats in rows.values():
        cols = [s["ColumnNo"] for s in row_seats]
        left_col, right_col = min(cols), max(cols)
        for s in row_seats:
            if s["ColumnNo"] in (left_col, right_col):
                window_seats.append(str(s["SeatLabel"]))

    # men_seats here really means "not reserved for women" (general seats).
    # women_seats are seats specifically flagged Gender == "F" by the KSRTC API.
    men_seats, women_seats = [], []
    for seat in seats:
        if seat.get("IsAvailable") == 1 and seat.get("SeatLabel"):
            label = str(seat.get("SeatLabel"))
            gender = seat.get("Gender")
            if gender == "F":
                women_seats.append(label)
            else:
                men_seats.append(label)

    available_bare = men_seats + women_seats
    window_seats = [s for s in window_seats if s in available_bare]

    return {
        "ok": True,
        "men_seats": men_seats,
        "women_seats": women_seats,
        "window_seats": window_seats,
    }


# ---------------------------------------------------------------------------
# SEAT SUGGESTION / VALIDATION (multi-seat, gender-aware)
# ---------------------------------------------------------------------------
def suggest_seats(count: int, window_seats: list, men_seats: list, women_seats: list,
                   user_gender: str, prefer_window: bool = True) -> list[str]:
    """Suggest up to `count` seats, respecting the gender restriction:
    a male user is never suggested a women-reserved seat.

    prefer_window=True  -> prioritize window seats (existing behaviour)
    prefer_window=False -> prioritize non-window (aisle/middle) seats,
                            for users whose booking history shows they
                            avoid window seats.
    """
    if user_gender == "F":
        eligible_all = men_seats + women_seats
    else:
        eligible_all = list(men_seats)

    if prefer_window:
        primary_pool = [s for s in window_seats if s in eligible_all]
    else:
        primary_pool = [s for s in eligible_all if s not in window_seats]

    chosen: list[str] = []
    pool = primary_pool[:]
    random.shuffle(pool)
    chosen.extend(pool[:count])

    if len(chosen) < count:
        remaining = [s for s in eligible_all if s not in chosen]
        random.shuffle(remaining)
        chosen.extend(remaining[: count - len(chosen)])

    return chosen[:count]


def recommend_seats(seat_info: dict, user_gender: str, static_preferences: str,
                     learned_seat_type: str | None, num_seats: int) -> list[str]:
    """Single source of truth for 'what seats should we suggest this user'.
    An explicit 'window seat' registration preference wins; otherwise fall
    back to what their booking history shows (needs 2+ past bookings).
    Returns [] if there's no signal either way."""
    static_pref = (static_preferences or "").lower()

    if "window seat" in static_pref or learned_seat_type == "window":
        prefer_window = True
    elif learned_seat_type == "aisle":
        prefer_window = False
    else:
        return []

    return suggest_seats(
        num_seats, seat_info["window_seats"], seat_info["men_seats"],
        seat_info["women_seats"], user_gender, prefer_window=prefer_window,
    )


def validate_booking(seat_numbers: list[str], user_gender: str, women_seats: list) -> dict:
    """Validates a list of seat numbers. Male users cannot book any seat
    reserved for women."""
    seat_numbers = [s.strip() for s in (seat_numbers or []) if s.strip()]
    if not seat_numbers:
        return {"ok": False, "error": "No seat numbers provided."}

    if len(set(seat_numbers)) != len(seat_numbers):
        return {"ok": False, "error": "Duplicate seat numbers in request."}

    if user_gender != "F":
        blocked = [s for s in seat_numbers if s in women_seats]
        if blocked:
            return {
                "ok": False,
                "error": f"Seat(s) {', '.join(blocked)} are reserved for women and can't be booked.",
            }

    return {"ok": True}