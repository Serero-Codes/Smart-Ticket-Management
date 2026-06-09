import os
import functools
import sqlite3
from datetime import datetime
from flask import Flask, render_template, request, redirect, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from classifier import classify_ticket
from ai_responder import generate_ticket_response
from forecasting import build_forecast
from governance import run_governance_audit, log_governance_event, init_governance_table
from workflow import (init_workflow_tables, run_ticket_workflow, run_status_workflow,
                      run_approval_workflow, get_sla_status, ROUTING_RULES,
                      run_escalation_check, bulk_approve, bulk_reject,
                      create_workflow_rule, update_workflow_rule, toggle_workflow_rule)

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

# Init governance + workflow tables
_gconn = get_db()
init_governance_table(_gconn)
init_workflow_tables(_gconn)
_gconn.close()


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
    # Governance audit log
    ticket_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    log_governance_event(conn, "ticket_submitted",
        f"Ticket #{ticket_id} — category={category}, confidence={round(confidence)}%, priority={priority}, dept={assigned_department}",
        session.get("username", "unknown"))
    conn.close()

    # ── Workflow automation pipeline ──
    _wconn = get_db()
    init_workflow_tables(_wconn)
    employee_email = _wconn.execute(
        "SELECT email FROM users WHERE id=?", (session["user_id"],)
    ).fetchone()
    employee_email = employee_email["email"] if employee_email else None
    run_ticket_workflow(_wconn, ticket_id, {
        "category": category, "priority": priority,
        "ai_response": ai_response, "ticket_text": ticket_text,
        "employee_name": session["username"],
    }, employee_email)
    _wconn.close()

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

    # ensure columns exist before querying
    existing_cols = [row[1] for row in conn.execute("PRAGMA table_info(tickets)").fetchall()]
    if "priority" not in existing_cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN priority TEXT DEFAULT 'Normal'")
    if "assigned_department" not in existing_cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN assigned_department TEXT")
    conn.commit()

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
    init_workflow_tables(conn)
    ticket = conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    note = request.form.get("note", "")
    conn.execute("UPDATE tickets SET status = ? WHERE id = ?", (new_status, ticket_id))
    conn.commit()
    if ticket:
        emp = conn.execute("SELECT email FROM users WHERE id=?", (ticket["user_id"],)).fetchone()
        emp_email = emp["email"] if emp else None
        run_status_workflow(conn, ticket_id, new_status,
                            employee_email=emp_email,
                            employee_name=ticket["employee_name"],
                            category=ticket["category"],
                            note=note)
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


# =========================
# FORECAST
# =========================

@app.route("/forecast")
@admin_required
def forecast():
    conn = get_db()
    existing_cols = [row[1] for row in conn.execute("PRAGMA table_info(tickets)").fetchall()]
    if "priority" not in existing_cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN priority TEXT DEFAULT 'Normal'")
    if "assigned_department" not in existing_cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN assigned_department TEXT")
    conn.commit()
    tickets = conn.execute("SELECT * FROM tickets ORDER BY created_at ASC").fetchall()
    conn.close()

    fc = build_forecast(tickets)
    return render_template("forecast.html",
                           username=session["username"],
                           fc=fc)


# =========================
# GOVERNANCE
# =========================

@app.route("/governance")
@admin_required
def governance():
    conn = get_db()
    init_governance_table(conn)
    existing_cols = [row[1] for row in conn.execute("PRAGMA table_info(tickets)").fetchall()]
    if "priority" not in existing_cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN priority TEXT DEFAULT 'Normal'")
    if "assigned_department" not in existing_cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN assigned_department TEXT")
    conn.commit()

    tickets   = conn.execute("SELECT * FROM tickets ORDER BY id DESC").fetchall()
    audit_log = conn.execute(
        "SELECT * FROM governance_log ORDER BY id DESC LIMIT 50"
    ).fetchall()
    conn.close()

    gov = run_governance_audit(tickets)

    # Log the audit run itself
    _gc = get_db()
    log_governance_event(_gc, "audit_run",
        f"Governance audit executed — risk score={gov['risk_score']}, "
        f"active_flags={len(gov['active_risks'])}, avg_confidence={gov['avg_confidence']}%",
        session.get("username", "admin"))
    _gc.close()

    return render_template("governance.html",
                           username=session["username"],
                           gov=gov,
                           audit_log=audit_log)



# =========================
# WORKFLOW AUTOMATION
# =========================

@app.route("/workflow")
@admin_required
def workflow():
    conn = get_db()
    init_workflow_tables(conn)

    tickets      = conn.execute("SELECT * FROM tickets ORDER BY id DESC").fetchall()
    approvals_db = conn.execute("""
        SELECT a.*, t.ticket_text, t.category, t.priority, t.assigned_department, t.sla_due
        FROM approvals a JOIN tickets t ON a.ticket_id = t.id
        ORDER BY a.id DESC LIMIT 10
    """).fetchall()
    notifications = conn.execute(
        "SELECT * FROM notifications ORDER BY id DESC LIMIT 30"
    ).fetchall()
    workflow_log = conn.execute(
        "SELECT * FROM workflow_log ORDER BY id DESC LIMIT 40"
    ).fetchall()
    conn.close()

    from datetime import datetime, timedelta
    now = datetime.now()

    pending_approvals = [dict(a) for a in approvals_db if a["status"] == "pending"]
    pending_count = len(pending_approvals)

    sla_breached = 0
    sla_tickets  = []
    for t in tickets:
        if t["status"] in ("Open", "In Progress") and t["sla_due"]:
            sla_info = get_sla_status(t["sla_due"])
            if sla_info["sla_class"] == "sla-breach":
                sla_breached += 1
            td = dict(t)
            td["sla_info"] = sla_info
            sla_tickets.append(td)

    sla_tickets.sort(key=lambda x: x["sla_due"] or "9999")

    approved_n  = sum(1 for a in approvals_db if a["status"] == "approved")

    kpis = {
        "total_automated":   len(tickets),
        "pending_approvals": pending_count,
        "approved":          approved_n,
        "sla_breached":      sla_breached,
        "notifications_sent": conn.execute if False else 0,
    }
    # reopen conn just for notifications count
    _c = get_db()
    kpis["notifications_sent"] = _c.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
    _c.close()

    import os as _os
    email_configured = bool(_os.environ.get("SMTP_USER") and _os.environ.get("SMTP_PASS"))

    return render_template("workflow.html",
                           username=session["username"],
                           kpis=kpis,
                           routing_rules=ROUTING_RULES,
                           pending_approvals=pending_approvals,
                           pending_count=pending_count,
                           sla_tickets=sla_tickets[:20],
                           notifications=notifications,
                           workflow_log=workflow_log,
                           email_configured=email_configured)


# =========================
# APPROVALS QUEUE
# =========================

@app.route("/approvals")
@admin_required
def approvals():
    conn = get_db()
    init_workflow_tables(conn)
    rows = conn.execute("""
        SELECT a.*, t.ticket_text, t.category, t.priority,
               t.assigned_department, t.sla_due
        FROM approvals a JOIN tickets t ON a.ticket_id = t.id
        ORDER BY CASE a.status WHEN 'pending' THEN 0 ELSE 1 END, a.id DESC
    """).fetchall()
    conn.close()

    total    = len(rows)
    pending  = sum(1 for r in rows if r["status"] == "pending")
    approved = sum(1 for r in rows if r["status"] == "approved")
    rejected = sum(1 for r in rows if r["status"] == "rejected")
    apr_rate = round(approved / (approved + rejected) * 100) if (approved + rejected) > 0 else 0

    return render_template("approvals.html",
                           username=session["username"],
                           approvals=rows,
                           stats={"total": total, "pending": pending,
                                  "approved": approved, "rejected": rejected,
                                  "approval_rate": apr_rate})


@app.route("/approve/<int:ticket_id>", methods=["POST"])
@admin_required
def approve_ticket(ticket_id):
    decision = request.form.get("decision")
    note     = request.form.get("note", "")
    if decision not in ("approved", "rejected"):
        return "Invalid decision", 400

    conn = get_db()
    init_workflow_tables(conn)
    ticket = conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    if ticket:
        emp = conn.execute(
            "SELECT email FROM users WHERE id=?", (ticket["user_id"],)
        ).fetchone()
        run_approval_workflow(conn, ticket_id, decision,
                              reviewer=session["username"],
                              employee_email=emp["email"] if emp else None,
                              employee_name=ticket["employee_name"],
                              category=ticket["category"],
                              note=note)
    conn.close()
    return redirect("/approvals")


# =========================
# WORKFLOW AUTOMATION APIs
# =========================

@app.route("/api/workflow/escalate", methods=["POST"])
@admin_required
def api_escalate():
    """Manually trigger escalation check for all SLA-breached tickets."""
    conn = get_db()
    init_workflow_tables(conn)
    escalated = run_escalation_check(conn, base_url=request.host_url.rstrip("/"))
    conn.close()
    return jsonify({"escalated": escalated, "count": len(escalated)})


@app.route("/api/workflow/bulk_approve", methods=["POST"])
@admin_required
def api_bulk_approve():
    data = request.get_json(silent=True) or {}
    ids  = [int(i) for i in (data.get("ticket_ids") or []) if str(i).isdigit()]
    note = data.get("note", "Bulk approved by admin")
    if not ids:
        return jsonify({"error": "No ticket IDs provided"}), 400
    conn = get_db()
    init_workflow_tables(conn)
    count = bulk_approve(conn, ids, reviewer=session["username"], note=note)
    conn.close()
    return jsonify({"approved": count})


@app.route("/api/workflow/bulk_reject", methods=["POST"])
@admin_required
def api_bulk_reject():
    data = request.get_json(silent=True) or {}
    ids  = [int(i) for i in (data.get("ticket_ids") or []) if str(i).isdigit()]
    note = data.get("note", "Bulk rejected by admin")
    if not ids:
        return jsonify({"error": "No ticket IDs provided"}), 400
    conn = get_db()
    init_workflow_tables(conn)
    count = bulk_reject(conn, ids, reviewer=session["username"], note=note)
    conn.close()
    return jsonify({"rejected": count})


@app.route("/api/workflow/rules", methods=["GET"])
@admin_required
def api_get_rules():
    conn = get_db()
    rows = conn.execute("SELECT * FROM workflow_rules ORDER BY id DESC").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/workflow/rules", methods=["POST"])
@admin_required
def api_create_rule():
    data = request.get_json(silent=True) or {}
    required = ["name", "category", "department", "sla_hours"]
    if not all(data.get(k) for k in required):
        return jsonify({"error": "Missing required fields"}), 400
    conn = get_db()
    init_workflow_tables(conn)
    create_workflow_rule(
        conn,
        name=data["name"],
        category=data["category"],
        department=data["department"],
        sla_hours=int(data.get("sla_hours", 24)),
        requires_approval=bool(data.get("requires_approval", False)),
        escalate_after_hrs=int(data.get("escalate_after_hrs", 48))
    )
    conn.close()
    return jsonify({"status": "created"})


@app.route("/api/workflow/rules/<int:rule_id>", methods=["PATCH"])
@admin_required
def api_update_rule(rule_id):
    data = request.get_json(silent=True) or {}
    conn = get_db()
    update_workflow_rule(
        conn, rule_id,
        sla_hours=int(data.get("sla_hours", 24)),
        requires_approval=bool(data.get("requires_approval", False)),
        escalate_after_hrs=int(data.get("escalate_after_hrs", 48))
    )
    conn.close()
    return jsonify({"status": "updated"})


@app.route("/api/workflow/rules/<int:rule_id>/toggle", methods=["POST"])
@admin_required
def api_toggle_rule(rule_id):
    data   = request.get_json(silent=True) or {}
    active = bool(data.get("active", True))
    conn   = get_db()
    toggle_workflow_rule(conn, rule_id, active)
    conn.close()
    return jsonify({"status": "toggled", "active": active})


@app.route("/api/workflow/stats")
@admin_required
def api_workflow_stats():
    conn = get_db()
    init_workflow_tables(conn)
    total_notif  = conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
    sent_notif   = conn.execute("SELECT COUNT(*) FROM notifications WHERE status='sent'").fetchone()[0]
    pending_appr = conn.execute("SELECT COUNT(*) FROM approvals WHERE status='pending'").fetchone()[0]
    escalations  = conn.execute("SELECT COUNT(*) FROM escalations").fetchone()[0]
    wf_events    = conn.execute("SELECT COUNT(*) FROM workflow_log").fetchone()[0]
    webhooks     = conn.execute("SELECT COUNT(*) FROM webhook_log").fetchone()[0] if _table_exists(conn, "webhook_log") else 0
    conn.close()
    return jsonify({
        "notifications_total":  total_notif,
        "notifications_sent":   sent_notif,
        "pending_approvals":    pending_appr,
        "escalations":          escalations,
        "workflow_events":      wf_events,
        "webhooks_fired":       webhooks,
    })


def _table_exists(conn, table_name):
    return conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
    ).fetchone() is not None


if __name__ == "__main__":
    app.run(host="0.0.0.0")
