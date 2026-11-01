from flask import Flask, jsonify, request, send_from_directory, session, redirect, url_for

from flask_cors import CORS
from html import escape
import math
import os
import time
import sqlite3
from functools import wraps
from werkzeug.security import check_password_hash, generate_password_hash

import requests


app = Flask(__name__)
CORS(app)
app.secret_key = os.getenv("SAFEDRIVE_SECRET_KEY") or os.urandom(32)

DATABASE_PATH = os.path.join(os.path.dirname(__file__), "safedrive.db")


def database():
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_database():
    with database() as connection:
        connection.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin', 'user'))
        )""")
        user_columns = {row["name"] for row in connection.execute("PRAGMA table_info(users)")}
        if "vehicle_password_hash" not in user_columns:
            connection.execute("ALTER TABLE users ADD COLUMN vehicle_password_hash TEXT")
        if not connection.execute("SELECT 1 FROM users WHERE role = 'admin'").fetchone():
            username = os.getenv("SAFEDRIVE_ADMIN_USERNAME", "admin")
            password = os.getenv("SAFEDRIVE_ADMIN_PASSWORD", "admin123")
            connection.execute("INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'admin')",
                               (username, generate_password_hash(password)))


def role_required(role):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if session.get("role") != role:
                return redirect(url_for("login"))
            return view(*args, **kwargs)
        return wrapped
    return decorator


init_database()

DEFAULT_LAT, DEFAULT_LON = 30.68114, 76.60584
MAPBOX_ACCESS_TOKEN = (
    os.getenv("MAPBOX_ACCESS_TOKEN")
    or os.getenv("VITE_MAPBOX_TOKEN")
    or "pk.eyJ1IjoiZXZhZWxsYWxsYWwiLCJhIjoiY204b2U5Nzg0MDFzbDJtcHAxdGN5MWl4NSJ9.H9WERlS25nf9tn-fzf_Fsw"
)
try:
    from telegram_config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_POLICE_CHAT_ID
except ModuleNotFoundError as exc:
    if exc.name != "telegram_config":
        raise
    TELEGRAM_BOT_TOKEN = "9087824810:AAG34jks3g4Vw9z9PSRCCg3lBubBCZrNWRc"
    TELEGRAM_CHAT_ID = "7912359149"
    TELEGRAM_POLICE_CHAT_ID = None


def telegram_dispatch_configured():
    def is_configured(value):
        if not value or not value.strip():
            return False
        return not value.strip().casefold().startswith(("your_", "replace_", "change_me"))

    return is_configured(TELEGRAM_BOT_TOKEN) and is_configured(TELEGRAM_CHAT_ID)


latest_event = {"status": "nominal"}
live_stream = {
    "g_force": 1.0, "smoke_adc": 380, "flame_detected": False,
    "latitude": DEFAULT_LAT, "longitude": DEFAULT_LON, "satellites": 0,
    "status": "MONITORING",
}

PENDING_WINDOW_SECONDS = 10


@app.route("/")
@role_required("admin")
def admin_dashboard():
    return send_from_directory(".", "index.html")


@app.route("/driver")
@role_required("user")
def driver_dashboard():
    session["vehicle_unlocked"] = False
    return send_from_directory(".", "driver.html")


def current_driver():
    if session.get("role") != "user":
        return None
    with database() as connection:
        return connection.execute(
            "SELECT vehicle_password_hash FROM users WHERE username = ? AND role = 'user'",
            (session.get("username"),),
        ).fetchone()


@app.route("/api/vehicle/access", methods=["GET", "POST"])
def vehicle_access():
    user = current_driver()
    if not user:
        return jsonify({"error": "Driver sign-in is required."}), 401

    if request.method == "GET":
        return jsonify({
            "password_configured": bool(user["vehicle_password_hash"]),
            "unlocked": bool(session.get("vehicle_unlocked") and user["vehicle_password_hash"]),
        })

    payload = request.get_json(silent=True) or {}
    password = payload.get("password")
    if not isinstance(password, str):
        return jsonify({"error": "Enter a vehicle access password."}), 400

    with database() as connection:
        stored = connection.execute(
            "SELECT vehicle_password_hash FROM users WHERE username = ? AND role = 'user'",
            (session.get("username"),),
        ).fetchone()
        if not stored:
            return jsonify({"error": "Driver account was not found. Please sign in again."}), 401

        if not stored["vehicle_password_hash"]:
            confirmation = payload.get("confirmation")
            if len(password) < 8:
                return jsonify({"error": "Create a vehicle password with at least 8 characters."}), 400
            if password != confirmation:
                return jsonify({"error": "The vehicle passwords do not match."}), 400
            connection.execute(
                "UPDATE users SET vehicle_password_hash = ? WHERE username = ? AND role = 'user'",
                (generate_password_hash(password), session.get("username")),
            )
        elif not check_password_hash(stored["vehicle_password_hash"], password):
            return jsonify({"error": "Incorrect vehicle password."}), 401

    session["vehicle_unlocked"] = True
    return jsonify({"status": "unlocked"})


@app.route("/api/vehicle/lock", methods=["POST"])
def lock_vehicle_panel():
    if session.get("role") != "user":
        return jsonify({"error": "Driver sign-in is required."}), 401
    session["vehicle_unlocked"] = False
    return jsonify({"status": "locked"})


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        with database() as connection:
            user = connection.execute("SELECT username, password_hash, role FROM users WHERE username = ?", (username,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session.update(username=user["username"], role=user["role"])
            return redirect(url_for("admin_dashboard" if user["role"] == "admin" else "driver_dashboard"))
        return send_from_directory(".", "login.html"), 401
    return send_from_directory(".", "login.html")


@app.route("/register", methods=["POST"])
def register():
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    if len(username) < 3 or len(password) < 8:
        return redirect(url_for("login", error="Use a 3+ character username and 8+ character password."))
    try:
        with database() as connection:
            connection.execute("INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'user')",
                               (username, generate_password_hash(password)))
    except sqlite3.IntegrityError:
        return redirect(url_for("login", error="That username is already used."))
    return redirect(url_for("login", message="Account created. Please sign in."))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def haversine_distance(lat1, lon1, lat2, lon2):
    radius = 6371.0
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return round(radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)), 2)


def find_nearest_place(lat, lon, query, keyword):
    response = requests.get(
        "https://api.mapbox.com/search/searchbox/v1/forward",
        params={"q": query, "proximity": f"{lon},{lat}", "types": "poi", "limit": 10,
                "country": "IN", "language": "en", "access_token": MAPBOX_ACCESS_TOKEN},
        timeout=5,
    )
    response.raise_for_status()
    features = response.json().get("features", [])
    places = []
    for feature in features:
        properties = feature.get("properties", {})
        coordinates = feature.get("geometry", {}).get("coordinates", [])
        name = properties.get("name", "")
        if not isinstance(name, str) or keyword not in name.casefold() or len(coordinates) < 2:
            continue
        place_lon, place_lat = coordinates[:2]
        if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in (place_lat, place_lon)):
            continue
        places.append({
            "name": name,
            "address": properties.get("full_address") or properties.get("place_formatted", ""),
            "lat": place_lat, "lon": place_lon,
            "distance_km": haversine_distance(lat, lon, place_lat, place_lon),
        })
    return min(places, key=lambda place: place["distance_km"], default=None)


def lookup_nearest(lat, lon, query, keyword, label):
    try:
        return find_nearest_place(lat, lon, query, keyword), None
    except (requests.RequestException, ValueError, TypeError) as exc:
        print(f"[-] {label} lookup failed: {type(exc).__name__}")
        return None, f"{label} lookup is currently unavailable."


def send_telegram(chat_id, message):
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        return False
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML", "disable_web_page_preview": False},
            timeout=4,
        )
        if response.status_code != 200:
            print(f"[-] Telegram alert rejected: HTTP {response.status_code}")
            return False
        result = response.json()
        if not isinstance(result, dict) or result.get("ok") is not True:
            description = result.get("description", "Telegram rejected the message") if isinstance(result, dict) else "Invalid Telegram response"
            print(f"[-] Telegram alert rejected: {description}")
            return False
        return True
    except ValueError:
        print("[-] Telegram alert returned an invalid response")
        return False
    except requests.RequestException as exc:
        print(f"[-] Telegram alert failed: {type(exc).__name__}")
        return False


def dispatch_sos(event, impact_g, smoke, lat, lon, hospital, police_station):
    maps_link = f"https://www.google.com/maps/dir/?api=1&destination={lat},{lon}&travelmode=driving"
    hospital_name = hospital["name"] if hospital else "No nearby hospital found"
    police_name = police_station["name"] if police_station else "No nearby police station found"
    message = (
        "🚨 <b>SAFEDRIVE AI: EMERGENCY SOS DISPATCH</b> 🚨\n\n"
        f"⚠️ <b>Incident Type:</b> {escape(str(event))}\n"
        f"💥 <b>Peak Deceleration:</b> {impact_g:.2f} G\n"
        f"💨 <b>Cabin Smoke Level:</b> {escape(str(smoke))} ADC\n"
        f"🏥 <b>Nearest Hospital:</b> {escape(hospital_name)}\n"
        f"👮 <b>Nearest Police Station:</b> {escape(police_name)}\n\n"
        f"📍 <b>Live Crash Navigation:</b> <a href='{maps_link}'>Tap to Open Route</a>"
    )
    return send_telegram(TELEGRAM_CHAT_ID, message), send_telegram(TELEGRAM_POLICE_CHAT_ID, message)


@app.route("/api/live_stream", methods=["GET", "POST"])
def handle_live_stream():
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        for key in ("g_force", "smoke_adc", "flame_detected", "latitude", "longitude", "satellites", "status"):
            if key in payload:
                live_stream[key] = payload[key]
        return jsonify({"status": "received"}), 200
    return jsonify(live_stream)


@app.route("/api/sos", methods=["POST"])
def send_manual_sos():
    if session.get("role") != "admin":
        return jsonify({"error": "Admin authentication is required to send an SOS."}), 401
    if not telegram_dispatch_configured():
        return jsonify({
            "error": "Telegram is not configured. Copy telegram_config.example.py to telegram_config.py, enter your credentials, then restart Flask."
        }), 503

    try:
        lat = float(live_stream["latitude"])
        lon = float(live_stream["longitude"])
        impact_g = float(live_stream["g_force"])
        smoke = live_stream["smoke_adc"]
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "Current telemetry is unavailable or invalid."}), 503
    if not all(math.isfinite(value) for value in (lat, lon, impact_g)) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return jsonify({"error": "Current telemetry contains an invalid location or impact value."}), 503

    maps_link = f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
    message = (
        "🚨 <b>SAFEDRIVE AI: MANUAL SOS</b> 🚨\n\n"
        "⚠️ <b>Incident:</b> SOS requested from the dashboard\n"
        f"💥 <b>Current impact:</b> {impact_g:.2f} G\n"
        f"💨 <b>Smoke:</b> {escape(str(smoke))} ADC\n\n"
        f"📍 <a href='{maps_link}'>Open current vehicle location</a>\n"
        f"<code>{lat:.6f}, {lon:.6f}</code>"
    )
    if not send_telegram(TELEGRAM_CHAT_ID, message):
        return jsonify({
            "error": "Telegram did not confirm delivery. Check the bot token and chat ID, open the bot in Telegram and press Start, then check the Flask terminal."
        }), 502
    return jsonify({"status": "sent"}), 200


@app.route("/api/telemetry", methods=["GET", "POST"])
def handle_telemetry():
    global latest_event
    if request.method == "GET":
        return jsonify(latest_event)
    payload = request.get_json(silent=True) or {}
    status = payload.get("status")
    if status == "pending":
        latest_event = {
            "status": "pending", "vehicle_id": payload.get("vehicle_id", "IND-PB-9842"),
            "event": payload.get("event", "Sensor alert"), "impact_g": float(payload.get("impact_g", 1.0)),
            "latitude": float(payload.get("latitude", DEFAULT_LAT)), "longitude": float(payload.get("longitude", DEFAULT_LON)),
            "smoke_val": payload.get("smoke_val", live_stream["smoke_adc"]),
            "deadline": int(time.time() * 1000) + PENDING_WINDOW_SECONDS * 1000,
            "timestamp": payload.get("timestamp") or int(time.time() * 1000),
        }
        return jsonify({"status": "verification_started", "window_seconds": PENDING_WINDOW_SECONDS}), 202
    if status == "aborted":
        latest_event = {"status": "aborted", "event": "False Trigger Cancelled by Driver", "timestamp": payload.get("timestamp") or int(time.time() * 1000)}
        return jsonify({"status": "aborted_ack"}), 200
    if status == "confirmed" and latest_event.get("status") == "aborted":
        return jsonify({"status": "cancelled"}), 409
    if status != "confirmed":
        return jsonify({"error": "Incident must be confirmed before dispatch."}), 400
    if latest_event.get("status") == "pending":
        milliseconds_left = latest_event.get("deadline", 0) - int(time.time() * 1000)
        if milliseconds_left > 0:
            return jsonify({
                "status": "verification_pending",
                "seconds_remaining": max(1, math.ceil(milliseconds_left / 1000)),
            }), 202

    try:
        lat, lon = float(payload.get("latitude", DEFAULT_LAT)), float(payload.get("longitude", DEFAULT_LON))
        impact_g = float(payload.get("impact_g", 1.0))
    except (TypeError, ValueError):
        return jsonify({"error": "latitude, longitude, and impact_g must be numeric"}), 400
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or not all(math.isfinite(value) for value in (lat, lon, impact_g)):
        return jsonify({"error": "invalid incident data"}), 400

    smoke, event = payload.get("smoke_val", live_stream["smoke_adc"]), payload.get("event", "Severe Impact Crash")
    triage = "CRITICAL (L3)" if impact_g > 4 else "ELEVATED (L2)" if impact_g > 2.5 else "MONITORING (L1)"
    hospital, hospital_error = lookup_nearest(lat, lon, "hospital", "hospital", "Hospital")
    police, police_error = lookup_nearest(lat, lon, "police station", "police", "Police-station")
    primary_sent, police_sent = dispatch_sos(event, impact_g, smoke, lat, lon, hospital, police)
    telegram_error = None
    if not primary_sent:
        telegram_error = (
            "Telegram is not configured. Copy telegram_config.example.py to telegram_config.py, enter your credentials, then restart Flask."
            if not telegram_dispatch_configured()
            else "Telegram did not confirm delivery. Check the bot token, chat ID, bot access, and Flask terminal."
        )
    latest_event = {
        "status": "dispatched", "vehicle_id": payload.get("vehicle_id", "IND-PB-9842"), "event": event,
        "impact_g": impact_g, "latitude": lat, "longitude": lon, "smoke_val": smoke, "triage_level": triage,
        "nearest_hospital": hospital, "hospital_lookup_error": hospital_error,
        "nearest_police_station": police, "police_lookup_error": police_error,
        "telegram_alert_sent": primary_sent, "telegram_error": telegram_error,
        "police_alert_sent": police_sent,
        "timestamp": payload.get("timestamp") or int(time.time() * 1000),
    }
    return jsonify(latest_event), 200 if primary_sent else 502


if __name__ == "__main__":
    if telegram_dispatch_configured():
        print("[CONFIG] Telegram dispatch is configured.")
    else:
        print("[CONFIG] Telegram is not configured. Copy telegram_config.example.py to telegram_config.py and enter your credentials.")
    app.run(host="0.0.0.0", port=5000, debug=False)
