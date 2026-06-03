import os
import functools
import sqlite3
from datetime import datetime
from flask import Flask, render_template, request, redirect, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from classifier import classify_ticket
from ai_responder import generate_ticket_response

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24))

DATABASE = os.environ.get("DATABASE_PATH", "database.db")


# =========================
# DATABASE INITIALIZATION
# =========================

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            department TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            created_at TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            employee_name TEXT NOT NULL,
            department TEXT NOT NULL,
            ticket_text TEXT NOT NULL,
            category TEXT NOT NULL,
            confidence REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'Open',
            ai_response TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    # Migrate existing DB — add ai_response column if missing
    existing_cols = [row[1] for row in cursor.execute("PRAGMA table_info(tickets)").fetchall()]
    if "ai_response" not in existing_cols:
        cursor.execute("ALTER TABLE tickets ADD COLUMN ai_response TEXT")

    # Seed a default admin if none exists
    existing = cursor.execute("SELECT id FROM users WHERE role='admin'").fetchone()
    if not existing:
        hashed = generate_password_hash("admin123")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("""
            INSERT OR IGNORE INTO users (username, email, password, department, role, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ("Admin", "admin@company.com", hashed, "IT", "admin", now))

    conn.commit()
    conn.close()


init_db()


# =========================
# AUTH DECORATOR
# =========================

def login_required(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect("/login")
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect("/login")
        if session.get("role") != "admin":
            return redirect("/")
        return f(*args, **kwargs)
    return wrapper


# =========================
# AUTH ROUTES
# =========================

@app.route("/")
def home():
    if "user_id" in session:
        return redirect("/dashboard" if session.get("role") == "admin" else "/index")
    return redirect("/login")


@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect("/")

    if request.method == "POST":
        data = request.get_json(silent=True) or request.form
        email = (data.get("email") or "").strip().lower()
        password = (data.get("password") or "").strip()

        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        conn.close()

        if user and check_password_hash(user["password"], password):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["email"] = user["email"]
            session["department"] = user["department"]
            session["role"] = user["role"]

            if request.is_json:
                return jsonify({"success": True, "redirect": "/dashboard" if user["role"] == "admin" else "/index"})
            return redirect("/dashboard" if user["role"] == "admin" else "/index")

        if request.is_json:
            return jsonify({"success": False, "message": "Invalid email or password"}), 401
        flash("Invalid email or password", "error")

    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"].strip()
        email = request.form["email"].strip().lower()
        password = request.form["password"].strip()
        department = request.form["department"]

        if not username or not email or not password or not department:
            flash("All fields are required.", "error")
            return render_template("register.html")

        hashed = generate_password_hash(password)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        conn = get_db()
        try:
            conn.execute("""
                INSERT INTO users (username, email, password, department, role, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (username, email, hashed, department, "user", now))
            conn.commit()
            flash("Account created! Please log in.", "success")
            return redirect("/login")
        except sqlite3.IntegrityError:
            flash("Email or username already registered.", "error")
        finally:
            conn.close()

    return render_template("register.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


# =========================
# EMPLOYEE ROUTES
# =========================

@app.route("/index")
@login_required
def index():
    popup = session.pop("ticket_popup", None)
    tone_error = session.pop("tone_error", None)
    ticket_draft = session.pop("ticket_draft", "")
    return render_template("index.html",
                           username=session["username"],
                           department=session["department"],
                           ticket_popup=popup,
                           tone_error=tone_error,
                           ticket_draft=ticket_draft)



# ── Category → correct department mapping ──
CATEGORY_DEPARTMENT = {
    "IT":         "IT",
    "HR":         "HR",
    "Finance":    "Finance",
    "Operations": "Operations",
}

# ── Profanity / informal-tone word list ──
BAD_TONE_WORDS = {
    "fuck", "shit", "ass", "bitch", "damn", "crap", "bastard", "hell",
    "idiot", "stupid", "wtf", "omg", "lol", "lmao", "wtf", "bs",
    "piss", "pissed", "bloody", "screw", "sucks", "dumb", "suck",
    "cunt", "dick", "cock", "asshole", "bullshit",
}

# ── Urgency keywords ──
URGENCY_KEYWORDS = {
    "urgent", "asap", "emergency", "immediately", "critical", "broken",
    "down", "cannot work", "can't work", "not working", "crashed",
    "deadline", "today", "right now", "help", "stuck", "blocked",
    "data loss", "lost data", "security", "breach", "hacked",
}

def check_tone(text: str):
    """Returns (is_clean: bool, offending_word: str|None)"""
    words = set(text.lower().split())
    for word in words:
        clean = word.strip(".,!?;:\"'()")
        if clean in BAD_TONE_WORDS:
            return False, clean
    return True, None

def detect_urgency(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in URGENCY_KEYWORDS)


@app.route("/submit", methods=["POST"])
@login_required
def submit_ticket():
    ticket_text = request.form.get("ticket_text", "").strip()
    if not ticket_text:
        flash("Ticket description cannot be empty.", "error")
        return redirect("/index")

    # ── Tone validation ──
    is_clean, bad_word = check_tone(ticket_text)
    if not is_clean:
        session["tone_error"] = (
            f"Please keep your ticket professional. "
            f"Informal or offensive language was detected. "
            f"Kindly revise your message and resubmit."
        )
        session["ticket_draft"] = ticket_text
        return redirect("/index")

    # ── Classify & detect urgency ──
    category, confidence = classify_ticket(ticket_text)
    is_urgent = detect_urgency(ticket_text)
    priority = "Urgent" if is_urgent else "Normal"

    # ── Correct department routing (based on ticket category, not user dept) ──
    assigned_department = CATEGORY_DEPARTMENT.get(category, category)

    ai_response = generate_ticket_response(ticket_text, category)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_db()

    # Add priority & assigned_department columns if they don't exist yet
    existing_cols = [row[1] for row in conn.execute("PRAGMA table_info(tickets)").fetchall()]
    if "priority" not in existing_cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN priority TEXT DEFAULT 'Normal'")
    if "assigned_department" not in existing_cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN assigned_department TEXT")

    conn.execute("""
        INSERT INTO tickets (user_id, employee_name, department, ticket_text, category,
                             confidence, status, ai_response, created_at, priority, assigned_department)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (session["user_id"], session["username"], session["department"],
          ticket_text, category, confidence, "Open", ai_response, now,
          priority, assigned_department))
    conn.commit()
    conn.close()

    session["ticket_popup"] = {
        "category": category,
        "assigned_department": assigned_department,
        "confidence": round(confidence),
        "ai_response": ai_response,
        "priority": priority,
        "ticket_text": ticket_text[:120] + ("…" if len(ticket_text) > 120 else "")
    }
    return redirect("/index")


@app.route("/history")
@login_required
def history():
    conn = get_db()
    tickets = conn.execute("""
        SELECT * FROM tickets WHERE user_id = ? ORDER BY id DESC
    """, (session["user_id"],)).fetchall()
    conn.close()
    return render_template("history.html", tickets=tickets, username=session["username"])


# =========================
# DEPARTMENT / ADMIN ROUTES
# =========================

@app.route("/department")
@login_required
def department_tickets():
    conn = get_db()
    current_user_id = session["user_id"]
    if session["role"] == "admin":
        tickets = conn.execute(
            "SELECT * FROM tickets WHERE user_id != ? ORDER BY id DESC",
            (current_user_id,)
        ).fetchall()
    else:
        tickets = conn.execute("""
            SELECT * FROM tickets WHERE assigned_department = ? AND user_id != ? ORDER BY id DESC
        """, (session["department"], current_user_id)).fetchall()
    conn.close()
    return render_template("department.html", tickets=tickets, username=session["username"], role=session["role"])


@app.route("/update_status/<int:ticket_id>", methods=["POST"])
@login_required
def update_status(ticket_id):
    new_status = request.form.get("status")
    valid_statuses = {"Open", "In Progress", "Closed"}
    if new_status not in valid_statuses:
        return "Invalid status", 400

    conn = get_db()
    conn.execute("UPDATE tickets SET status = ? WHERE id = ?", (new_status, ticket_id))
    conn.commit()
    conn.close()
    return redirect("/department")


@app.route("/dashboard")
@admin_required
def dashboard():
    conn = get_db()
    tickets = conn.execute("SELECT * FROM tickets ORDER BY id DESC").fetchall()
    total = len(tickets)
    open_count = sum(1 for t in tickets if t["status"] == "Open")
    in_progress = sum(1 for t in tickets if t["status"] == "In Progress")
    closed = sum(1 for t in tickets if t["status"] == "Closed")

    categories = {}
    for t in tickets:
        categories[t["category"]] = categories.get(t["category"], 0) + 1

    users = conn.execute("SELECT COUNT(*) as count FROM users").fetchone()["count"]
    conn.close()

    return render_template("dashboard.html",
                           tickets=tickets,
                           total=total,
                           open_count=open_count,
                           in_progress=in_progress,
                           closed=closed,
                           categories=categories,
                           users=users,
                           username=session["username"])


# =========================
# API: STATS (JSON)
# =========================

@app.route("/api/stats")
@admin_required
def api_stats():
    conn = get_db()
    tickets = conn.execute("SELECT * FROM tickets").fetchall()
    conn.close()
    categories = {}
    statuses = {"Open": 0, "In Progress": 0, "Closed": 0}
    for t in tickets:
        categories[t["category"]] = categories.get(t["category"], 0) + 1
        statuses[t["status"]] = statuses.get(t["status"], 0) + 1
    return jsonify({"categories": categories, "statuses": statuses, "total": len(tickets)})


if __name__ == "__main__":
    app.run(host="0.0.0.0")
