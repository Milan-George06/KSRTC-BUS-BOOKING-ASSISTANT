import requests
import streamlit as st

BACKEND_URL = "http://localhost:8000"

st.set_page_config(page_title="KSRTC Bus Booking", page_icon="🚌", layout="centered")

if "session_id" not in st.session_state:
    st.session_state.session_id = None
    st.session_state.user = None
    st.session_state.chat_history = []
    st.session_state.pending_trip = None   # last /chat "buses" response, currently shown
    st.session_state.last_bus_list = None  # same, kept around so "back" works after selecting a bus
    st.session_state.auto_suggestion = None  # {"bus_number","service_type","seats",...} or None
    st.session_state.seat_info = None      # last /buses/select response
    st.session_state.selected_bus = None   # bus dict the seat_info belongs to


def api_post(path: str, payload: dict):
    try:
        resp = requests.post(f"{BACKEND_URL}{path}", json=payload)
    except requests.exceptions.ConnectionError:
        st.error("Can't reach the backend. Is it running on http://localhost:8000 ?")
        st.stop()

    if resp.status_code >= 400:
        try:
            error_data = resp.json()
            error_msg = error_data.get("detail", "Something went wrong.")
        except ValueError:
            # If the response isn't JSON, show the raw text and the status code
            error_msg = f"Backend Error {resp.status_code}: {resp.text}"

        st.error(error_msg)
        return None

    return resp.json()


st.title("🚌 KSRTC Bus Booking")

# ---------- AUTH ----------
if not st.session_state.session_id:
    tab_login, tab_register = st.tabs(["Login", "Register"])

    with tab_login:
        with st.form("login_form"):
            username = st.text_input("Username", key="login_user")
            password = st.text_input("Password", type="password", key="login_pass")
            submitted = st.form_submit_button("Login")
            if submitted:
                data = api_post("/auth/login", {"username": username, "password": password})
                if data:
                    st.session_state.session_id = data["session_id"]
                    st.session_state.user = data["user"]
                    st.session_state.chat_history = [{
                        "role": "assistant",
                        "content": (
                            f"Hey {data['user']['Username']}! Where would you like to travel? "
                            "(e.g. 'from Kottayam to Ernakulam tomorrow evening, 2 seats')"
                        ),
                    }]
                    st.rerun()

    with tab_register:
        with st.form("register_form"):
            r_username = st.text_input("Choose a username")
            r_password = st.text_input("Set a password", type="password")
            r_gender = st.selectbox("Gender", ["M", "F"])
            r_pref = st.text_input("Preferences (e.g. 'window seat')", value="")
            submitted = st.form_submit_button("Register")
            if submitted:
                data = api_post("/auth/register", {
                    "username": r_username, "password": r_password,
                    "gender": r_gender, "preferences": r_pref,
                })
                if data:
                    st.session_state.session_id = data["session_id"]
                    st.session_state.user = data["user"]
                    st.session_state.chat_history = [{
                        "role": "assistant",
                        "content": f"Welcome {data['user']['Username']}! Where would you like to travel?",
                    }]
                    st.rerun()

    st.stop()

# ---------- LOGGED IN ----------
user = st.session_state.user
st.sidebar.success(f"Logged in as {user['Username']}")
if st.sidebar.button("Log out"):
    st.session_state.session_id = None
    st.session_state.user = None
    st.session_state.chat_history = []
    st.session_state.pending_trip = None
    st.session_state.last_bus_list = None
    st.session_state.auto_suggestion = None
    st.session_state.seat_info = None
    st.session_state.selected_bus = None
    st.rerun()

# ---------- CHAT HISTORY ----------
for msg in st.session_state.chat_history:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# ---------- AUTO-SUGGESTED BUS + SEAT(S) (once we know their preferences) ----------
if st.session_state.auto_suggestion:
    sug = st.session_state.auto_suggestion
    with st.container(border=True):
        st.markdown(f"⭐ **Recommended for you:** {sug['service_type']}")
        fare_line = f"Departs: {sug['departure_time']} · Fare: ₹{sug['fare']}"
        if sug["num_seats"] > 1:
            fare_line += f" × {sug['num_seats']} = ₹{sug['fare'] * sug['num_seats']} total"
        fare_line += f" · Seat(s): {', '.join(sug['seats'])}"
        st.write(fare_line)

        col_book, col_seat, col_other = st.columns(3)
        with col_book:
            if st.button("✅ Book this"):
                data = api_post("/buses/book", {
                    "session_id": st.session_state.session_id,
                    "seat_numbers": sug["seats"],
                })
                if data:
                    st.session_state.chat_history.append({
                        "role": "assistant",
                        "content": (
                            f"✅ {data['status']} — Seat(s) {', '.join(data['seat_numbers'])} on "
                            f"{data['service_type']} (₹{data['total_fare']} total, departs {data['departure_time']}).\n\n"
                            "Anything else I can help you book?"
                        ),
                    })
                    st.session_state.auto_suggestion = None
                    st.session_state.pending_trip = None
                    st.session_state.last_bus_list = None
                    st.rerun()
        with col_seat:
            if st.button("Change seat"):
                data = api_post("/buses/select", {
                    "session_id": st.session_state.session_id,
                    "bus_number": sug["bus_number"],
                })
                if data:
                    st.session_state.seat_info = data
                    st.session_state.selected_bus = {
                        "number": sug["bus_number"],
                        "service_type": sug["service_type"],
                        "departure_time": sug["departure_time"],
                        "fare": sug["fare"],
                    }
                    st.session_state.auto_suggestion = None
                    st.session_state.pending_trip = None
                    st.rerun()
        with col_other:
            if st.button("Choose a different bus"):
                st.session_state.auto_suggestion = None
                st.rerun()

# ---------- BUS LIST (from the last /chat "buses" response) ----------
elif st.session_state.pending_trip:
    trip = st.session_state.pending_trip
    for bus in trip["buses"]:
        with st.container(border=True):
            cols = st.columns([3, 1])
            with cols[0]:
                st.markdown(f"**{bus['number']}. {bus['service_type']}**")
                st.write(f"Departs: {bus['departure_time']} · Fare: ₹{bus['fare']} · "
                         f"Seats left: {bus['available_seats']}")
            with cols[1]:
                if st.button("Select", key=f"select_{bus['number']}"):
                    data = api_post("/buses/select", {
                        "session_id": st.session_state.session_id,
                        "bus_number": bus["number"],
                    })
                    if data:
                        st.session_state.seat_info = data
                        st.session_state.selected_bus = bus
                        st.session_state.pending_trip = None
                        st.rerun()

# ---------- SEAT SELECTION / BOOKING ----------
if st.session_state.seat_info:
    info = st.session_state.seat_info
    bus = st.session_state.selected_bus
    num_seats = info["num_seats"]

    st.subheader(f"{bus['service_type']} · Departs {bus['departure_time']} · ₹{bus['fare']}/seat")

    if info["women_only_seats"]:
        st.caption(f"Reserved for women (not selectable for you): {', '.join(info['women_only_seats'])}")

    if info["suggested_seats"]:
        st.info(f"Suggested seat(s) for you: **{', '.join(info['suggested_seats'])}**")

    if not info["selectable_seats"]:
        st.warning("No seats currently available for you on this bus.")
        chosen = []
    else:
        default_seats = [s for s in (info["suggested_seats"] or info["selectable_seats"][:num_seats])
                          if s in info["selectable_seats"]]
        chosen = st.multiselect(
            f"Choose {num_seats} seat(s)",
            options=info["selectable_seats"],
            default=default_seats,
        )

    col_book, col_back = st.columns(2)
    with col_book:
        book_clicked = st.button("Book seat(s)", disabled=not info["selectable_seats"])
    with col_back:
        back_clicked = st.button("⬅ Back to bus list")

    if book_clicked:
        if len(chosen) != num_seats:
            st.error(f"Please select exactly {num_seats} seat(s).")
        else:
            data = api_post("/buses/book", {
                "session_id": st.session_state.session_id,
                "seat_numbers": chosen,
            })
            if data:
                st.session_state.chat_history.append({
                    "role": "assistant",
                    "content": (
                        f"✅ {data['status']} — Seat(s) {', '.join(data['seat_numbers'])} on "
                        f"{data['service_type']} (₹{data['total_fare']} total, departs {data['departure_time']}).\n\n"
                        "Anything else I can help you book?"
                    ),
                })
                st.session_state.seat_info = None
                st.session_state.selected_bus = None
                st.session_state.pending_trip = None
                st.session_state.last_bus_list = None
                st.rerun()

    if back_clicked:
        st.session_state.pending_trip = st.session_state.last_bus_list
        st.session_state.seat_info = None
        st.session_state.selected_bus = None
        st.rerun()

# ---------- CHAT INPUT (always available, including right after a booking) ----------
prompt = st.chat_input(
    "Type a message… e.g. 'hi', or 'book 2 seats from Kottayam to Ernakulam tomorrow evening'"
)
if prompt:
    st.session_state.chat_history.append({"role": "user", "content": prompt})
    data = api_post("/chat", {"session_id": st.session_state.session_id, "message": prompt})
    if data:
        if data["type"] == "buses":
            if data.get("note"):
                st.session_state.chat_history.append({"role": "assistant", "content": data["note"]})
            st.session_state.chat_history.append({
                "role": "assistant",
                "content": (
                    f"Found {len(data['buses'])} bus(es) from {data['from']} to {data['to']} "
                    f"on {data['date']} for {data['num_seats']} seat(s). Pick one below:"
                ),
            })
            st.session_state.pending_trip = data
            st.session_state.last_bus_list = data
            st.session_state.auto_suggestion = data.get("auto_suggestion")
            st.session_state.seat_info = None
            st.session_state.selected_bus = None
        else:
            st.session_state.chat_history.append({"role": "assistant", "content": data["message"]})
        st.rerun()
    # if data is None, api_post() already rendered st.error() above - don't
    # rerun, or that error gets wiped before you ever see it.