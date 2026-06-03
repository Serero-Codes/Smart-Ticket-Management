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



# =========================
# ANALYTICS DASHBOARD
# =========================

@app.route("/analytics")
@admin_required
def analytics():
    from datetime import datetime, timedelta
    conn = get_db()

    existing_cols = [row[1] for row in conn.execute("PRAGMA table_info(tickets)").fetchall()]
    if "priority" not in existing_cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN priority TEXT DEFAULT 'Normal'")
    if "assigned_department" not in existing_cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN assigned_department TEXT")
    conn.commit()

    tickets = conn.execute("SELECT * FROM tickets ORDER BY id DESC").fetchall()
    users   = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()

    total    = len(tickets)
    closed_n = sum(1 for t in tickets if t["status"] == "Closed")
    open_n   = sum(1 for t in tickets if t["status"] == "Open")
    in_prog  = sum(1 for t in tickets if t["status"] == "In Progress")
    urgent_n = sum(1 for t in tickets if (t["priority"] or "Normal") == "Urgent")
    normal_n = total - urgent_n
    res_rate = round(closed_n / total * 100) if total else 0

    now = datetime.now()
    week_ago = now - timedelta(days=7)
    this_week = 0
    for t in tickets:
        try:
            ts = datetime.strptime(t["created_at"], "%Y-%m-%d %H:%M:%S")
            if ts >= week_ago:
                this_week += 1
        except Exception:
            pass

    kpis = {
        "total": total, "closed": closed_n, "open": open_n,
        "in_progress": in_prog, "urgent": urgent_n, "normal": normal_n,
        "resolution_rate": res_rate, "this_week": this_week,
        "urgent_pct": round(urgent_n / total * 100) if total else 0,
        "users": users,
    }

    volume_map = {}
    for i in range(29, -1, -1):
        day = (now - timedelta(days=i)).strftime("%b %d")
        volume_map[day] = 0
    for t in tickets:
        try:
            day = datetime.strptime(t["created_at"], "%Y-%m-%d %H:%M:%S").strftime("%b %d")
            if day in volume_map:
                volume_map[day] += 1
        except Exception:
            pass
    volume_labels = list(volume_map.keys())
    volume_data   = list(volume_map.values())

    cat_map = {}
    for t in tickets:
        cat_map[t["category"]] = cat_map.get(t["category"], 0) + 1
    cat_labels = list(cat_map.keys())
    cat_data   = list(cat_map.values())

    depts = ["IT", "HR", "Finance", "Operations"]
    dept_stats = []
    for d in depts:
        dt = [t for t in tickets if (t["assigned_department"] or t["category"]) == d]
        if not dt:
            continue
        dc = len(dt)
        dd = sum(1 for t in dt if t["status"] == "Closed")
        do = sum(1 for t in dt if t["status"] == "Open")
        du = sum(1 for t in dt if (t["priority"] or "Normal") == "Urgent")
        dept_stats.append({
            "name": d, "total": dc, "closed": dd, "open": do, "urgent": du,
            "share": round(dc / total * 100) if total else 0,
            "resolution": round(dd / dc * 100) if dc else 0,
        })

    return render_template("analytics.html",
                           username=session["username"],
                           kpis=kpis,
                           volume_labels=volume_labels,
                           volume_data=volume_data,
                           cat_labels=cat_labels,
                           cat_data=cat_data,
                           dept_stats=dept_stats)


# =========================
# WEEKLY REPORT
# =========================

@app.route("/report")
@admin_required
def weekly_report():
    from datetime import datetime, timedelta
    conn = get_db()

    existing_cols = [row[1] for row in conn.execute("PRAGMA table_info(tickets)").fetchall()]
    if "priority" not in existing_cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN priority TEXT DEFAULT 'Normal'")
    if "assigned_department" not in existing_cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN assigned_department TEXT")
    conn.commit()

    tickets = conn.execute("SELECT * FROM tickets ORDER BY id DESC").fetchall()
    users   = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()

    now      = datetime.now()
    week_ago = now - timedelta(days=7)

    total    = len(tickets)
    closed_n = sum(1 for t in tickets if t["status"] == "Closed")
    open_n   = sum(1 for t in tickets if t["status"] == "Open")
    urgent_n = sum(1 for t in tickets if (t["priority"] or "Normal") == "Urgent")
    res_rate = round(closed_n / total * 100) if total else 0

    this_week = 0
    for t in tickets:
        try:
            if datetime.strptime(t["created_at"], "%Y-%m-%d %H:%M:%S") >= week_ago:
                this_week += 1
        except Exception:
            pass

    depts = ["IT", "HR", "Finance", "Operations"]
    dept_stats = []
    for d in depts:
        dt = [t for t in tickets if (t["assigned_department"] or t["category"]) == d]
        if not dt:
            continue
        dc = len(dt)
        dd = sum(1 for t in dt if t["status"] == "Closed")
        do = sum(1 for t in dt if t["status"] == "Open")
        du = sum(1 for t in dt if (t["priority"] or "Normal") == "Urgent")
        dr = round(dd / dc * 100) if dc else 0

        if dr >= 80:
            perf = f"<strong>{d}</strong> is performing well with a <strong>{dr}% resolution rate</strong>."
        elif dr >= 50:
            perf = f"<strong>{d}</strong> has a moderate resolution rate of <strong>{dr}%</strong> — there is room for improvement."
        else:
            perf = f"<strong>{d}</strong> has a low resolution rate of <strong>{dr}%</strong> and requires attention."

        urg_note  = f" {du} ticket{'s' if du != 1 else ''} flagged as urgent." if du else " No urgent tickets this period."
        open_note = f" {do} ticket{'s' if do != 1 else ''} remain{'s' if do == 1 else ''} open." if do else " All tickets resolved."

        dept_stats.append({
            "name": d, "total": dc, "closed": dd, "open": do,
            "urgent": du, "resolution": dr,
            "insight": perf + urg_note + open_note,
        })

    cat_map = {}
    for t in tickets:
        cat_map[t["category"]] = cat_map.get(t["category"], 0) + 1
    top_cat   = max(cat_map, key=cat_map.get) if cat_map else "N/A"
    top_cat_n = cat_map.get(top_cat, 0)

    if res_rate >= 75:
        overall = (f"The platform is operating at a <strong>high efficiency level</strong> with a "
                   f"<strong>{res_rate}% resolution rate</strong> across {total} total tickets. ")
    elif res_rate >= 50:
        overall = (f"The platform shows <strong>moderate performance</strong> with a "
                   f"<strong>{res_rate}% resolution rate</strong>. Management attention is recommended for open backlogs. ")
    else:
        overall = (f"Platform performance is <strong>below target</strong>. The resolution rate stands at "
                   f"<strong>{res_rate}%</strong> — escalation is advised. ")

    overall += (f"<strong>{this_week} new ticket{'s' if this_week != 1 else ''}</strong> were submitted this week. "
                f"The highest-volume category is <strong>{top_cat}</strong> with {top_cat_n} ticket{'s' if top_cat_n != 1 else ''}. ")
    if urgent_n:
        overall += f"<strong>{urgent_n} urgent ticket{'s' if urgent_n != 1 else ''}</strong> require immediate resolution. "
    else:
        overall += "No urgent tickets are currently outstanding. "

    week_start = (now - timedelta(days=6)).strftime("%d %b")
    week_end   = now.strftime("%d %b %Y")
    week_label = f"{week_start} – {week_end}"

    report = {
        "week_label":     week_label,
        "generated_at":   now.strftime("%d %B %Y, %H:%M"),
        "total":          total,
        "resolution_rate": res_rate,
        "urgent":         urgent_n,
        "this_week":      this_week,
        "users":          users,
        "insights":       overall,
        "dept_stats":     dept_stats,
        "recent_tickets": tickets[:25],
    }

    return render_template("report.html",
                           username=session["username"],
                           report=report)

if __name__ == "__main__":
    app.run(host="0.0.0.0")
