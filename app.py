from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
import sqlite3
import os
from functools import wraps
from datetime import datetime

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "change-this-secret-key")

# Local PC: keep "users.db"
# Render Free: change to "/tmp/users.db" so it can write
DB_NAME = os.environ.get("DB_NAME", "users.db")

def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()

    # users table (plain text password as you requested)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Map ESP32 MAC -> room_id
    cur.execute("""
        CREATE TABLE IF NOT EXISTS device_map (
            mac TEXT PRIMARY KEY,
            room_id INTEGER NOT NULL
        )
    """)

    # Store meter readings
    cur.execute("""
        CREATE TABLE IF NOT EXISTS meter_readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mac TEXT NOT NULL,
            room_id INTEGER NOT NULL,
            kwh REAL,
            voltage REAL,
            current REAL,
            power REAL,
            pf REAL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # room state table for your room control page
    cur.execute("""
        CREATE TABLE IF NOT EXISTS room_state (
            room_id INTEGER PRIMARY KEY,
            l1 INTEGER DEFAULT 0,
            l2 INTEGER DEFAULT 0,
            l3 INTEGER DEFAULT 0,
            l4 INTEGER DEFAULT 0,
            projector INTEGER DEFAULT 0,
            ac INTEGER DEFAULT 0,
            temperature INTEGER DEFAULT 16,
            swing_v INTEGER DEFAULT 0,
            swing_h INTEGER DEFAULT 0,
            fan_speed INTEGER DEFAULT 1,
            mode TEXT DEFAULT 'cool'
        )
    """)

    conn.commit()
    conn.close()

def normalize_mac(mac: str) -> str:
    if not mac:
        return ""
    mac = mac.strip().lower().replace("-", ":")
    # optional: remove spaces
    mac = mac.replace(" ", "")
    return mac

def get_room_id_for_mac(mac: str) -> int | None:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT room_id FROM device_map WHERE mac = ?", (mac,))
    row = cur.fetchone()
    conn.close()
    return int(row["room_id"]) if row else None

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper

@app.route("/")
def home():
    return redirect(url_for("dashboard") if "user_id" in session else url_for("login"))

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not username or not password:
            flash("Username and password are required.")
            return redirect(url_for("register"))

        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, password))
            conn.commit()
            conn.close()
            flash("Registered. Please log in.")
            return redirect(url_for("login"))
        except sqlite3.IntegrityError:
            flash("Username already exists.")
            return redirect(url_for("register"))

    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id, username, password FROM users WHERE username = ?", (username,))
        user = cur.fetchone()
        conn.close()

        if user and user["password"] == password:
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            return redirect(url_for("dashboard"))

        flash("Invalid username or password.")
        return redirect(url_for("login"))

    return render_template("login.html")

# ========= AFTER LOGIN: rooms list =========
@app.route("/dashboard")
@login_required
def dashboard():
    rooms = [{"id": i, "name": f"Room {i}"} for i in range(1, 15)]
    return render_template("rooms.html", rooms=rooms, username=session.get("username"))

# ========= room control page =========
@app.route("/room/<int:room_id>")
@login_required
def room(room_id):
    conn = get_db()
    cur = conn.cursor()

    # ensure row exists
    cur.execute("INSERT OR IGNORE INTO room_state (room_id) VALUES (?)", (room_id,))
    conn.commit()

    cur.execute("SELECT * FROM room_state WHERE room_id = ?", (room_id,))
    row = cur.fetchone()
    conn.close()

    state = dict(row) if row else {
        "room_id": room_id,
        "l1":0,"l2":0,"l3":0,"l4":0,
        "projector":0,"ac":0,
        "temperature":16,"swing_v":0,"swing_h":0,
        "fan_speed":1,"mode":"cool"
    }

    kwh_labels = list(range(1, 13))
    kwh_values = [42, 48, 53, 58, 45, 55, 40, 22, 44, 60, 48, 80]

    return render_template(
        "room.html",
        room_id=room_id,
        state=state,
        kwh_labels=kwh_labels,
        kwh_values=kwh_values
    )


# ========= APIs for buttons =========
@app.post("/api/room/<int:room_id>/toggle")
@login_required
def toggle_device(room_id):
    data = request.get_json(force=True)
    device = data.get("device")

    allowed = {"l1","l2","l3","l4","projector","ac","swing_v","swing_h"}
    if device not in allowed:
        return jsonify({"ok": False, "error": "Invalid device"}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO room_state (room_id) VALUES (?)", (room_id,))
    cur.execute(
        f"UPDATE room_state SET {device} = CASE {device} WHEN 1 THEN 0 ELSE 1 END WHERE room_id = ?",
        (room_id,)
    )
    conn.commit()
    cur.execute("SELECT * FROM room_state WHERE room_id = ?", (room_id,))
    state = dict(cur.fetchone())
    conn.close()
    return jsonify({"ok": True, "state": state})

@app.post("/api/room/<int:room_id>/ac")
@login_required
def update_ac(room_id):
    data = request.get_json(force=True)

    temp = int(data.get("temperature", 16))
    temp = max(16, min(30, temp))

    fan_speed = int(data.get("fan_speed", 1))
    fan_speed = max(1, min(5, fan_speed))

    mode = data.get("mode", "cool")
    if mode not in {"cool", "auto", "dry", "fan"}:
        mode = "cool"

    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO room_state (room_id) VALUES (?)", (room_id,))
    cur.execute("""
        UPDATE room_state
        SET temperature=?, fan_speed=?, mode=?
        WHERE room_id=?
    """, (temp, fan_speed, mode, room_id))
    conn.commit()
    cur.execute("SELECT * FROM room_state WHERE room_id = ?", (room_id,))
    state = dict(cur.fetchone())
    conn.close()
    return jsonify({"ok": True, "state": state})

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/api/meter", methods=["POST"])
def api_meter():
    """
    ESP32 sends JSON:
    {
      "mac": "AA:BB:CC:DD:EE:FF",
      "kwh": 12.34,
      "voltage": 230.1,
      "current": 1.23,
      "power": 250.5,
      "pf": 0.98
    }
    """
    data = request.get_json(silent=True) or {}

    mac = normalize_mac(data.get("mac", ""))
    if not mac:
        return jsonify({"ok": False, "error": "mac is required"}), 400

    # If room_id is not sent, we look up mapping by mac
    room_id = data.get("room_id", None)
    if room_id is None:
        room_id = get_room_id_for_mac(mac)

    # If the mac is not mapped yet, return error (or you can auto-assign)
    if room_id is None:
        return jsonify({"ok": False, "error": f"MAC not mapped: {mac}"}), 400

    # Read values (optional)
    def to_float(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    kwh = to_float(data.get("kwh"))
    voltage = to_float(data.get("voltage"))
    current = to_float(data.get("current"))
    power = to_float(data.get("power"))
    pf = to_float(data.get("pf"))

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO meter_readings (mac, room_id, kwh, voltage, current, power, pf)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (mac, int(room_id), kwh, voltage, current, power, pf))
    conn.commit()
    conn.close()

    return jsonify({"ok": True, "mac": mac, "room_id": int(room_id)})

@app.route("/admin/map-device", methods=["POST"])
def map_device():
    # POST form-data: mac=aa:bb:cc:dd:ee:ff&room_id=1
    mac = normalize_mac(request.form.get("mac", ""))
    room_id = request.form.get("room_id", "").strip()

    if not mac or not room_id.isdigit():
        return "mac and numeric room_id required", 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO device_map (mac, room_id) VALUES (?, ?)", (mac, int(room_id)))
    conn.commit()
    conn.close()
    return "OK", 200

# Run DB init even with waitress
init_db()

if __name__ == "__main__":
    app.run(debug=True)
