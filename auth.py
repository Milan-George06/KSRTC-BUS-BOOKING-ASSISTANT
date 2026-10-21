import json
import os
import hashlib
import secrets
from collections import Counter

USERS_FILE = os.path.join(os.path.dirname(__file__), "users.json")


def _load_users() -> dict:
    if not os.path.exists(USERS_FILE):
        return {}
    with open(USERS_FILE, "r") as fp:
        return json.load(fp)


def _save_users(users: dict) -> None:
    with open(USERS_FILE, "w") as fp:
        json.dump(users, fp, indent=2)


def _hash_password(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode()).hexdigest()


def register_user(username: str, password: str, gender: str, preferences: str) -> dict:
    """Returns {'ok': True, 'user': {...}} or {'ok': False, 'error': str}"""
    users = _load_users()
    if username in users:
        return {"ok": False, "error": "Username already exists."}

    salt = secrets.token_hex(8)
    users[username] = {
        "password_hash": _hash_password(password, salt),
        "salt": salt,
        "Gender": gender.strip().upper(),
        "Preferences": preferences,
        "Bookings": [],
    }
    _save_users(users)

    return {
        "ok": True,
        "user": {
            "Username": username,
            "Gender": users[username]["Gender"],
            "Preferences": preferences,
        },
    }


def login_user(username: str, password: str) -> dict:
    users = _load_users()
    record = users.get(username)
    if not record:
        return {"ok": False, "error": "User not found. Please register first."}

    if _hash_password(password, record["salt"]) != record["password_hash"]:
        return {"ok": False, "error": "Incorrect password."}

    return {
        "ok": True,
        "user": {
            "Username": username,
            "Gender": record["Gender"],
            "Preferences": record["Preferences"],
        },
    }


# ---------------------------------------------------------------------------
# BOOKING HISTORY / LEARNED PREFERENCES
# ---------------------------------------------------------------------------
def record_booking(username: str, entry: dict) -> None:
    """Appends a booking record (e.g. {'time_of_day': 'evening',
    'seat_type': 'window', 'date': '2026-08-05'}) to the user's history,
    persisted in users.json so it carries across logins."""
    users = _load_users()
    if username not in users:
        return
    users[username].setdefault("Bookings", []).append(entry)
    _save_users(users)


def infer_preferences(username: str) -> dict:
    """Once a user has 2+ recorded bookings, returns their most common
    time_of_day and seat_type so the chatbot can start auto-suggesting.
    Returns {'time_of_day': None, 'seat_type': None} until then, or for
    any category with no clear signal."""
    users = _load_users()
    record = users.get(username) or {}
    bookings = record.get("Bookings", [])

    if len(bookings) < 2:
        return {"time_of_day": None, "seat_type": None}

    times = Counter(b["time_of_day"] for b in bookings if b.get("time_of_day"))
    seats = Counter(b["seat_type"] for b in bookings if b.get("seat_type"))

    return {
        "time_of_day": times.most_common(1)[0][0] if times else None,
        "seat_type": seats.most_common(1)[0][0] if seats else None,
    }