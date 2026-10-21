from pydantic import BaseModel


class RegisterRequest(BaseModel):
    username: str
    password: str
    gender: str          # "M" or "F"
    preferences: str = ""  # e.g. "window seat"


class LoginRequest(BaseModel):
    username: str
    password: str


class ChatRequest(BaseModel):
    session_id: str
    message: str          # free text: small talk, route info, or slot answers


class SelectBusRequest(BaseModel):
    session_id: str
    bus_number: int        # 1-indexed, matches the list returned by /chat


class BookRequest(BaseModel):
    session_id: str
    seat_numbers: list[str]
