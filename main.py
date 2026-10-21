import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uuid
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

import auth
import bus_service
from schemas import RegisterRequest, LoginRequest, ChatRequest, SelectBusRequest, BookRequest

app = FastAPI(title="KSRTC Booking API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # local dev only - tighten before deploying publicly
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory session store. Fine for a single-instance demo app.
# session_id -> {user, slots, buses, current_bus, men_seats, women_seats, window_seats}
SESSIONS: dict[str, dict] = {}


def _new_search_state() -> dict:
    """Fresh slot / booking state. Called on login and after a completed
    booking so the chat can immediately continue with a new search."""
    return {
        "slots": {"from_city": None, "to_city": None, "date": None, "num_seats": 1, "time_of_day": None},
        "buses": [],
        "current_bus": None,
        "men_seats": [],
        "women_seats": [],
        "window_seats": [],
    }


def _get_session(session_id: str) -> dict:
    session = SESSIONS.get(session_id)
    if not session:
        raise HTTPException(status_code=401, detail="Invalid or expired session. Please log in again.")
    return session


@app.post("/auth/register")
def register(req: RegisterRequest):
    result = auth.register_user(req.username, req.password, req.gender, req.preferences)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["error"])

    session_id = str(uuid.uuid4())
    SESSIONS[session_id] = {"user": result["user"], **_new_search_state()}
    return {"session_id": session_id, "user": result["user"]}


@app.post("/auth/login")
def login(req: LoginRequest):
    result = auth.login_user(req.username, req.password)
    if not result["ok"]:
        raise HTTPException(status_code=401, detail=result["error"])

    session_id = str(uuid.uuid4())
    SESSIONS[session_id] = {"user": result["user"], **_new_search_state()}
    return {"session_id": session_id, "user": result["user"]}


@app.post("/chat")
def chat(req: ChatRequest):
    """Single conversational entrypoint: handles small talk, fills in
    missing from/to/date/num_seats/time_of_day across turns, and triggers
    a bus search once all required slots are known."""
    session = _get_session(req.session_id)
    message = req.message.strip()

    if not message:
        return {"type": "need_info", "message": "Go ahead, ask me anything about booking a bus!"}

    small_talk_kind = bus_service.classify_small_talk(message)
    if small_talk_kind:
        return {
            "type": "smalltalk",
            "message": bus_service.small_talk_reply(small_talk_kind, session["user"]["Username"]),
        }

    slots = session["slots"]

    # Resolve relative date phrases ("tomorrow", "day after tomorrow",
    # "after 3 days", "in 3 days") and time-of-day words deterministically,
    # before handing the message to the LLM.
    resolved_message, resolved_date = bus_service.resolve_relative_date(message)
    time_of_day = bus_service.extract_time_of_day(message)

    try:
        parsed = bus_service.parse_query(resolved_message, known=slots)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    for key in ("from_city", "to_city", "date", "num_seats"):
        if parsed.get(key):
            slots[key] = parsed[key]

    # Regex-resolved relative date wins over whatever the LLM produced,
    # since it's exact date arithmetic rather than an LLM guess.
    if resolved_date:
        slots["date"] = resolved_date
    if time_of_day:
        slots["time_of_day"] = time_of_day

    # Reject dates that have already passed, without ever hitting the
    # backend/search call - just ask again and keep the chat going.
    if slots.get("date") and bus_service.is_past_date(slots["date"]):
        slots["date"] = None
        return {
            "type": "need_info",
            "message": (
                "That date's already passed. Please enter a valid date "
                "(today or later), e.g. 2026-08-05."
            ),
        }

    missing = bus_service.missing_slots(slots)
    if missing:
        return {
            "type": "need_info",
            "message": bus_service.build_need_info_message(missing, slots),
        }

    result = bus_service.search_buses(slots["from_city"], slots["to_city"], slots["date"])
    if not result["ok"]:
        raise HTTPException(status_code=404, detail=result["error"])

    session["current_bus"] = None

    buses_out_all = [
        {
            "number": i,
            "service_type": bus["ServiceType"],
            "departure_time": bus["DepartureTime"].split("T")[1],
            "fare": bus["Fare"],
            "available_seats": bus["AvailableSeats"],
        }
        for i, bus in enumerate(result["buses"], start=1)
    ]

    if not buses_out_all:
        return {
            "type": "need_info",
            "message": (
                f"No buses found from {slots['from_city']} to {slots['to_city']} "
                f"on {slots['date']}. Try a different date or route?"
            ),
        }

    # Apply the time-of-day filter, if the user asked for one this turn (or
    # a prior turn). If nothing matches that window, fall back to showing
    # every bus for the day with a note, rather than a dead end.
    requested_time = slots.get("time_of_day")
    note = None
    if requested_time:
        pairs = list(zip(buses_out_all, result["buses"]))
        filtered = [
            (b, raw) for b, raw in pairs
            if bus_service.time_of_day_bucket(b["departure_time"]) == requested_time
        ]
        if filtered:
            buses_out = [p[0] for p in filtered]
            raw_buses = [p[1] for p in filtered]
            for i, b in enumerate(buses_out, start=1):
                b["number"] = i
        else:
            buses_out = buses_out_all
            raw_buses = result["buses"]
            note = f"No buses found in the {requested_time} — here are all the buses for {slots['date']} instead."
    else:
        buses_out = buses_out_all
        raw_buses = result["buses"]

    session["buses"] = raw_buses
    # time_of_day is only meant to filter the search it was mentioned in;
    # clear it so it doesn't silently keep filtering unrelated future searches.
    slots["time_of_day"] = None

    # Once the user has 2+ recorded bookings, build one concrete proposal:
    # the bus that matches their usual travel-time bucket, PLUS the actual
    # seat(s) they'd likely want on it - not just a badge in the list.
    num_seats = slots.get("num_seats") or 1
    user = session["user"]
    prefs = auth.infer_preferences(user["Username"])
    auto_suggestion = None

    print(f"[auto_suggest] user={user['Username']} learned_prefs={prefs}")

    if prefs.get("time_of_day"):
        match = next(
            (
                (b_out, b_raw) for b_out, b_raw in zip(buses_out, raw_buses)
                if bus_service.time_of_day_bucket(b_out["departure_time"]) == prefs["time_of_day"]
            ),
            None,
        )
        if not match:
            print(f"[auto_suggest] no bus in this search matches preferred bucket '{prefs['time_of_day']}' "
                  f"(available buckets: {[bus_service.time_of_day_bucket(b['departure_time']) for b in buses_out]})")
        if match:
            b_out, b_raw = match
            print(f"[auto_suggest] matched bus #{b_out['number']} ({b_out['departure_time']}), fetching seat map...")
            seat_info = bus_service.get_seat_arrangement(b_raw["RouteScheduleId"], slots["date"])
            if not seat_info["ok"]:
                print(f"[auto_suggest] seat arrangement fetch FAILED: {seat_info.get('error')}")
            if seat_info["ok"]:
                selectable = (
                    seat_info["men_seats"] + seat_info["women_seats"]
                    if user["Gender"] == "F" else list(seat_info["men_seats"])
                )
                suggested = bus_service.recommend_seats(
                    seat_info, user["Gender"], user.get("Preferences", ""),
                    prefs.get("seat_type"), num_seats,
                )
                if not suggested:
                    # No clear window/aisle signal yet - still propose a bus,
                    # just with a plain default pick rather than no seats at all.
                    suggested = bus_service.suggest_seats(
                        num_seats, seat_info["window_seats"], seat_info["men_seats"],
                        seat_info["women_seats"], user["Gender"], prefer_window=True,
                    )
                print(f"[auto_suggest] suggested={suggested} selectable_count={len(selectable)} "
                      f"needed={num_seats}")

                if len(suggested) == num_seats and all(s in selectable for s in suggested):
                    auto_suggestion = {
                        "bus_number": b_out["number"],
                        "service_type": b_out["service_type"],
                        "departure_time": b_out["departure_time"],
                        "fare": b_out["fare"],
                        "num_seats": num_seats,
                        "seats": suggested,
                    }
                    # Pre-load session state so "Book this" can call
                    # /buses/book directly, without a separate select step.
                    session["current_bus"] = b_out["number"]
                    session["men_seats"] = seat_info["men_seats"]
                    session["women_seats"] = seat_info["women_seats"]
                    session["window_seats"] = seat_info["window_seats"]
                    print(f"[auto_suggest] SUCCESS -> auto_suggestion built for bus #{b_out['number']}")
                else:
                    print("[auto_suggest] suggested seats didn't satisfy count/selectable check - no auto_suggestion")

    return {
        "type": "buses",
        "from": slots["from_city"],
        "to": slots["to_city"],
        "date": slots["date"],
        "num_seats": num_seats,
        "buses": buses_out,
        "auto_suggestion": auto_suggestion,
        "note": note,
    }


@app.post("/buses/select")
def select_bus(req: SelectBusRequest):
    session = _get_session(req.session_id)
    buses = session.get("buses") or []

    if not buses or req.bus_number < 1 or req.bus_number > len(buses):
        raise HTTPException(status_code=400, detail="Invalid bus number.")

    bus = buses[req.bus_number - 1]
    route_id = bus["RouteScheduleId"]

    seat_info = bus_service.get_seat_arrangement(route_id, session["slots"]["date"])
    if not seat_info["ok"]:
        raise HTTPException(status_code=502, detail=seat_info["error"])

    session["current_bus"] = req.bus_number
    session["men_seats"] = seat_info["men_seats"]
    session["women_seats"] = seat_info["women_seats"]
    session["window_seats"] = seat_info["window_seats"]

    user = session["user"]
    num_seats = session["slots"].get("num_seats") or 1

    # Seats this user is actually allowed to pick from. A male user never
    # even sees women-reserved seats as selectable.
    if user["Gender"] == "F":
        selectable = seat_info["men_seats"] + seat_info["women_seats"]
    else:
        selectable = list(seat_info["men_seats"])

    # Seat suggestion: an explicit "window seat" preference always wins;
    # otherwise fall back to what their booking history shows (needs 2+
    # past bookings). If they usually avoid window seats, suggest a
    # non-window seat instead of leaving it unsuggested.
    learned = auth.infer_preferences(user["Username"])
    suggested = bus_service.recommend_seats(
        seat_info, user["Gender"], user.get("Preferences", ""), learned.get("seat_type"), num_seats,
    )

    return {
        "service_type": bus["ServiceType"],
        "departure_time": bus["DepartureTime"].split("T")[1],
        "fare": bus["Fare"],
        "num_seats": num_seats,
        "selectable_seats": sorted(selectable, key=lambda s: (len(s), s)),
        "women_only_seats": seat_info["women_seats"] if user["Gender"] != "F" else [],
        "window_seats": seat_info["window_seats"],
        "suggested_seats": suggested,
    }


@app.post("/buses/book")
def book(req: BookRequest):
    session = _get_session(req.session_id)

    if not session.get("current_bus"):
        raise HTTPException(status_code=400, detail="Choose a ride first before booking.")

    user = session["user"]
    validation = bus_service.validate_booking(req.seat_numbers, user["Gender"], session["women_seats"])
    if not validation["ok"]:
        raise HTTPException(status_code=400, detail=validation["error"])

    bus = session["buses"][session["current_bus"] - 1]
    booked = [s.strip() for s in req.seat_numbers]

    # Learn from this booking: which time-of-day bucket and seat type
    # (window vs aisle) the user actually went with, so future searches
    # can start auto-suggesting once there are 2+ of these on file.
    departure_time = bus["DepartureTime"].split("T")[1]
    time_bucket = bus_service.time_of_day_bucket(departure_time)
    window_count = sum(1 for s in booked if s in session.get("window_seats", []))
    seat_type = "window" if window_count > len(booked) / 2 else "aisle"
    auth.record_booking(user["Username"], {
        "time_of_day": time_bucket,
        "seat_type": seat_type,
        "date": session["slots"]["date"],
    })

    response = {
        "status": "Booking Successful",
        "service_type": bus["ServiceType"],
        "departure_time": departure_time,
        "fare": bus["Fare"],
        "total_fare": bus["Fare"] * len(booked),
        "seat_numbers": booked,
    }

    # Reset the search/seat state so the chat can immediately continue
    # with a new search instead of getting stuck after this booking.
    session.update(_new_search_state())

    return response


@app.get("/health")
def health():
    return {"status": "ok"}