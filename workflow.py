"""
workflow.py  —  End-to-End Workflow Automation Engine
=====================================================
Features:
  • Auto-routing rules (category → department + SLA)
  • Approval workflow triggers  (urgent + HR + Finance)
  • Email notification dispatch (SMTP / simulation)
  • Escalation engine          (SLA-breach auto-escalation)
  • Webhook integration        (POST to external URLs on events)
  • Workflow rule management   (custom rules stored in DB)
  • Bulk-action support        (approve/reject many tickets at once)
  • Notification log           (full history)
  • Workflow event log         (full audit trail)
"""

import os
import json
import smtplib
import threading
import urllib.request
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart


# ═══════════════════════════════════════════════════════════════════
# DEFAULT ROUTING RULES (overridden by DB rules if present)
# ═══════════════════════════════════════════════════════════════════

ROUTING_RULES = {
    "IT": {
        "department":         "IT",
        "sla_hours":          8,
        "requires_approval":  False,
        "approval_threshold": "Urgent",
        "escalate_after_hrs": 24,
        "notify_roles":       ["admin"],
        "icon":               "💻",
    },
    "HR": {
        "department":         "HR",
        "sla_hours":          24,
        "requires_approval":  True,
        "approval_threshold": "Normal",
        "escalate_after_hrs": 48,
        "notify_roles":       ["admin"],
        "icon":               "👥",
    },
    "Finance": {
        "department":         "Finance",
        "sla_hours":          48,
        "requires_approval":  True,
        "approval_threshold": "Normal",
        "escalate_after_hrs": 72,
        "notify_roles":       ["admin"],
        "icon":               "💰",
    },
    "Operations": {
        "department":         "Operations",
        "sla_hours":          16,
        "requires_approval":  False,
        "approval_threshold": "Urgent",
        "escalate_after_hrs": 32,
        "notify_roles":       ["admin"],
        "icon":               "⚙️",
    },
}

DEFAULT_RULE = {
    "department": "IT", "sla_hours": 24,
    "requires_approval": False, "approval_threshold": "Urgent",
    "escalate_after_hrs": 48, "notify_roles": ["admin"], "icon": "🎫",
}

# Webhook events — set via env var WEBHOOK_URL
WEBHOOK_URL    = os.environ.get("WEBHOOK_URL", "")
WEBHOOK_EVENTS = {"ticket_submitted", "approval_requested",
                  "approval_decision", "status_changed", "sla_breached"}


# ═══════════════════════════════════════════════════════════════════
# DB INITIALISATION
# ═══════════════════════════════════════════════════════════════════

def init_workflow_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id    INTEGER,
            recipient    TEXT NOT NULL,
            subject      TEXT NOT NULL,
            body         TEXT NOT NULL,
            channel      TEXT DEFAULT 'email',
            status       TEXT DEFAULT 'sent',
            created_at   TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS approvals (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id       INTEGER NOT NULL UNIQUE,
            status          TEXT NOT NULL DEFAULT 'pending',
            requested_by    TEXT,
            reviewed_by     TEXT,
            review_note     TEXT,
            requested_at    TEXT NOT NULL,
            reviewed_at     TEXT,
            FOREIGN KEY (ticket_id) REFERENCES tickets(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS workflow_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id   INTEGER,
            event       TEXT NOT NULL,
            detail      TEXT,
            created_at  TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS workflow_rules (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            name             TEXT NOT NULL,
            category         TEXT NOT NULL,
            department       TEXT NOT NULL,
            sla_hours        INTEGER NOT NULL DEFAULT 24,
            requires_approval INTEGER NOT NULL DEFAULT 0,
            escalate_after_hrs INTEGER NOT NULL DEFAULT 48,
            is_active        INTEGER NOT NULL DEFAULT 1,
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS escalations (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id   INTEGER NOT NULL UNIQUE,
            escalated_at TEXT NOT NULL,
            reason      TEXT,
            notified    INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS webhook_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event       TEXT NOT NULL,
            payload     TEXT,
            status      TEXT DEFAULT 'sent',
            created_at  TEXT NOT NULL
        )
    """)
    # Columns on tickets
    existing = [r[1] for r in conn.execute("PRAGMA table_info(tickets)").fetchall()]
    for col, defn in [
        ("sla_due",         "TEXT"),
        ("workflow_status", "TEXT DEFAULT 'active'"),
        ("escalated",       "INTEGER DEFAULT 0"),
        ("assigned_agent",  "TEXT"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE tickets ADD COLUMN {col} {defn}")
    conn.commit()


# ═══════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════

def log_workflow(conn, ticket_id, event, detail=""):
    conn.execute("""
        INSERT INTO workflow_log (ticket_id, event, detail, created_at)
        VALUES (?, ?, ?, ?)
    """, (ticket_id, event, detail, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()


def save_notification(conn, ticket_id, recipient, subject, body, status="sent", channel="email"):
    conn.execute("""
        INSERT INTO notifications (ticket_id, recipient, subject, body, channel, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (ticket_id, recipient, subject, body, channel,
          status, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()


def get_effective_rule(category: str) -> dict:
    """Return ROUTING_RULES entry or default."""
    return ROUTING_RULES.get(category, DEFAULT_RULE)


# ═══════════════════════════════════════════════════════════════════
# WEBHOOK
# ═══════════════════════════════════════════════════════════════════

def _fire_webhook(conn, event: str, payload: dict):
    """POST JSON payload to WEBHOOK_URL in a background thread."""
    if not WEBHOOK_URL or event not in WEBHOOK_EVENTS:
        return
    body = json.dumps({"event": event, "timestamp": datetime.now().isoformat(), **payload})

    def _post():
        status = "failed"
        try:
            req = urllib.request.Request(
                WEBHOOK_URL,
                data=body.encode(),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                status = "sent" if resp.status < 300 else "failed"
        except Exception as exc:
            print(f"[webhook] {exc}")
        try:
            conn.execute("""
                INSERT INTO webhook_log (event, payload, status, created_at)
                VALUES (?, ?, ?, ?)
            """, (event, body, status, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit()
        except Exception:
            pass

    threading.Thread(target=_post, daemon=True).start()


# ═══════════════════════════════════════════════════════════════════
# EMAIL CONFIG + SENDER
# ═══════════════════════════════════════════════════════════════════

EMAIL_CONFIG = {
    "smtp_host":  os.environ.get("SMTP_HOST",     "smtp.gmail.com"),
    "smtp_port":  int(os.environ.get("SMTP_PORT", "587")),
    "username":   os.environ.get("SMTP_USER",     ""),
    "password":   os.environ.get("SMTP_PASS",     ""),
    "from_name":  "Smart Ticket System",
    "from_addr":  os.environ.get("SMTP_USER",     "noreply@smarttickets.com"),
}


def _send_email(to: str, subject: str, html_body: str) -> bool:
    cfg = EMAIL_CONFIG
    if not cfg["username"] or not cfg["password"]:
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"{cfg['from_name']} <{cfg['from_addr']}>"
        msg["To"]      = to
        msg.attach(MIMEText(html_body, "html"))
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=10) as server:
            server.ehlo(); server.starttls()
            server.login(cfg["username"], cfg["password"])
            server.sendmail(cfg["from_addr"], to, msg.as_string())
        return True
    except Exception as exc:
        print(f"[email] {exc}")
        return False


def _send_async(to, subject, html_body):
    threading.Thread(target=_send_email, args=(to, subject, html_body), daemon=True).start()


# ═══════════════════════════════════════════════════════════════════
# EMAIL TEMPLATES
# ═══════════════════════════════════════════════════════════════════

def _base_email(title: str, content: str, cta_label: str = "", cta_url: str = "") -> str:
    cta_html = ""
    if cta_label and cta_url:
        cta_html = f"""
        <div style="text-align:center;margin:28px 0 8px">
            <a href="{cta_url}" style="display:inline-block;padding:12px 28px;
                background:#2563eb;color:#fff;font-weight:700;font-size:14px;
                border-radius:8px;text-decoration:none;
                box-shadow:0 2px 8px rgba(37,99,235,0.35)">{cta_label}</a>
        </div>"""
    return f"""
    <div style="font-family:'Segoe UI',Arial,sans-serif;max-width:560px;margin:0 auto;
                background:#f8fafc;padding:32px 16px">
      <div style="background:#fff;border-radius:12px;padding:32px;
                  border:1px solid #e2e8f0;box-shadow:0 2px 12px rgba(0,0,0,0.06)">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:24px;
                    padding-bottom:18px;border-bottom:1px solid #e2e8f0">
          <div style="width:34px;height:34px;background:#2563eb;border-radius:8px;
                      display:flex;align-items:center;justify-content:center;
                      color:#fff;font-weight:800;font-size:13px">ST</div>
          <span style="font-weight:800;font-size:15px;color:#0f172a">Smart Ticket System</span>
        </div>
        <h2 style="font-size:20px;font-weight:800;color:#0f172a;margin:0 0 16px;
                   letter-spacing:-0.02em">{title}</h2>
        {content}
        {cta_html}
        <p style="font-size:11px;color:#94a3b8;margin-top:28px;padding-top:16px;
                  border-top:1px solid #f1f5f9">
          Automated message from Smart Ticket Management System.<br>
          Do not reply to this email.
        </p>
      </div>
    </div>"""


def email_ticket_submitted(ticket_id, category, priority, ai_response, employee_name, dept):
    content = f"""
    <p style="color:#475569;line-height:1.7;margin:0 0 14px">
        Hi <strong style="color:#0f172a">{employee_name}</strong>,
    </p>
    <p style="color:#475569;line-height:1.7;margin:0 0 18px">
        Your ticket has been received and automatically routed to the
        <strong style="color:#0f172a">{dept}</strong> team.
    </p>
    <div style="background:#f1f5f9;border-radius:10px;padding:16px 18px;margin-bottom:18px;
                border-left:3px solid #2563eb">
      <table style="width:100%;font-size:13px;color:#475569">
        <tr><td style="padding:3px 0;font-weight:600;color:#64748b;width:120px">Ticket #</td>
            <td style="color:#0f172a;font-weight:700">#{ticket_id}</td></tr>
        <tr><td style="padding:3px 0;font-weight:600;color:#64748b">Category</td>
            <td style="color:#0f172a">{category}</td></tr>
        <tr><td style="padding:3px 0;font-weight:600;color:#64748b">Priority</td>
            <td style="color:{'#ef4444' if priority=='Urgent' else '#10b981'};font-weight:700">{priority}</td></tr>
        <tr><td style="padding:3px 0;font-weight:600;color:#64748b">Routed to</td>
            <td style="color:#0f172a">{dept}</td></tr>
      </table>
    </div>
    <p style="font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;
              color:#94a3b8;margin:0 0 8px">AI-Generated Response</p>
    <p style="color:#475569;line-height:1.7;margin:0;font-size:13px;
              padding:14px 16px;background:#f8fafc;border-radius:8px;
              border:1px solid #e2e8f0">{ai_response}</p>"""
    return _base_email("Ticket Submitted Successfully ✓", content)


def email_approval_required(ticket_id, category, priority, ticket_text,
                             requested_by, base_url="http://localhost:5000"):
    snippet = ticket_text[:120] + ("…" if len(ticket_text) > 120 else "")
    content = f"""
    <p style="color:#475569;line-height:1.7;margin:0 0 14px">
        A new <strong style="color:{'#ef4444' if priority=='Urgent' else '#f59e0b'}">{priority}</strong>
        ticket in the <strong style="color:#0f172a">{category}</strong> category
        requires your approval before it can proceed.
    </p>
    <div style="background:#fefce8;border:1px solid #fde047;border-radius:10px;
                padding:14px 16px;margin-bottom:18px;border-left:3px solid #f59e0b">
      <p style="font-size:12px;font-weight:700;color:#92400e;margin:0 0 6px;
                text-transform:uppercase;letter-spacing:0.06em">Ticket #{ticket_id}</p>
      <p style="font-size:13px;color:#78350f;margin:0;line-height:1.6;font-style:italic">
        "{snippet}"</p>
    </div>
    <p style="color:#64748b;font-size:13px;margin:0 0 6px">
        Submitted by: <strong style="color:#0f172a">{requested_by}</strong>
    </p>"""
    return _base_email("⚠️ Approval Required", content,
                        cta_label="Review & Approve →",
                        cta_url=f"{base_url}/approvals")


def email_status_update(ticket_id, new_status, category, employee_name, note=""):
    colour = {"Open": "#06b6d4", "In Progress": "#f59e0b", "Closed": "#10b981"}.get(new_status, "#94a3b8")
    note_html = f'<p style="color:#475569;font-size:13px;line-height:1.6;margin:12px 0 0;font-style:italic">Note: {note}</p>' if note else ""
    content = f"""
    <p style="color:#475569;line-height:1.7;margin:0 0 14px">
        Hi <strong style="color:#0f172a">{employee_name}</strong>,
        your ticket <strong>#{ticket_id}</strong> ({category}) has been updated.
    </p>
    <div style="text-align:center;padding:24px;background:#f8fafc;
                border-radius:10px;margin-bottom:18px;border:1px solid #e2e8f0">
        <span style="font-size:13px;color:#64748b;font-weight:600;
                     text-transform:uppercase;letter-spacing:0.08em">New Status</span><br>
        <span style="font-size:28px;font-weight:800;color:{colour};letter-spacing:-0.02em">
            {new_status}
        </span>
    </div>{note_html}"""
    return _base_email(f"Ticket #{ticket_id} Status Updated", content)


def email_approval_decision(ticket_id, decision, category, employee_name, reviewer, note=""):
    colour = "#10b981" if decision == "approved" else "#ef4444"
    icon   = "✅" if decision == "approved" else "❌"
    content = f"""
    <p style="color:#475569;line-height:1.7;margin:0 0 14px">
        Hi <strong style="color:#0f172a">{employee_name}</strong>,
        your <strong>{category}</strong> ticket <strong>#{ticket_id}</strong>
        has been reviewed.
    </p>
    <div style="text-align:center;padding:24px;background:#f8fafc;
                border-radius:10px;margin-bottom:18px;border:1px solid #e2e8f0">
        <span style="font-size:32px">{icon}</span><br>
        <span style="font-size:22px;font-weight:800;color:{colour};letter-spacing:-0.02em;text-transform:capitalize">
            {decision}
        </span>
        <p style="font-size:12px;color:#94a3b8;margin:6px 0 0">
            Reviewed by {reviewer}
        </p>
    </div>
    {"<p style='color:#475569;font-size:13px;font-style:italic;line-height:1.6'>Note: " + note + "</p>" if note else ""}"""
    return _base_email(f"Ticket #{ticket_id} {'Approved ✓' if decision=='approved' else 'Requires Changes'}", content)


def email_escalation_alert(ticket_id, category, priority, employee_name,
                            hours_overdue, base_url="http://localhost:5000"):
    content = f"""
    <p style="color:#475569;line-height:1.7;margin:0 0 14px">
        🚨 <strong style="color:#ef4444">SLA Breach Alert</strong> — Ticket
        <strong>#{ticket_id}</strong> ({category}) has exceeded its SLA window
        by <strong style="color:#ef4444">{hours_overdue:.1f} hours</strong>.
    </p>
    <div style="background:#fef2f2;border:1px solid #fecaca;border-radius:10px;
                padding:14px 16px;margin-bottom:18px;border-left:3px solid #ef4444">
      <table style="width:100%;font-size:13px;color:#7f1d1d">
        <tr><td style="padding:3px 0;font-weight:600;width:120px">Ticket #</td>
            <td style="font-weight:700">#{ticket_id}</td></tr>
        <tr><td style="padding:3px 0;font-weight:600">Category</td><td>{category}</td></tr>
        <tr><td style="padding:3px 0;font-weight:600">Priority</td>
            <td style="color:#ef4444;font-weight:700">{priority}</td></tr>
        <tr><td style="padding:3px 0;font-weight:600">Submitted by</td><td>{employee_name}</td></tr>
        <tr><td style="padding:3px 0;font-weight:600">Overdue by</td>
            <td style="color:#ef4444;font-weight:700">{hours_overdue:.1f}h</td></tr>
      </table>
    </div>
    <p style="color:#64748b;font-size:13px;margin:0">
        Immediate action is required. Please review and resolve this ticket.
    </p>"""
    return _base_email("🚨 SLA Breach — Immediate Action Required", content,
                        cta_label="View Ticket →",
                        cta_url=f"{base_url}/department")


# ═══════════════════════════════════════════════════════════════════
# MAIN WORKFLOW PIPELINE
# ═══════════════════════════════════════════════════════════════════

def run_ticket_workflow(conn, ticket_id: int, ticket: dict,
                        employee_email: str = None,
                        base_url: str = "http://localhost:5000"):
    """
    Full automation pipeline for a newly submitted ticket:
      1. Determine routing rule → set SLA due date
      2. Create approval request if required
      3. Send confirmation email to employee
      4. Send approval-required email to admin
      5. Fire webhook
      6. Log all events
    """
    category = ticket.get("category", "IT")
    priority = ticket.get("priority", "Normal")
    rule     = get_effective_rule(category)
    now      = datetime.now()

    # 1. SLA
    sla_due = (now + timedelta(hours=rule["sla_hours"])).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("UPDATE tickets SET sla_due = ? WHERE id = ?", (sla_due, ticket_id))
    conn.commit()
    log_workflow(conn, ticket_id, "sla_set",
                 f"SLA due: {sla_due} ({rule['sla_hours']}h window, dept={rule['department']})")

    # 2. Approval
    needs_approval = rule["requires_approval"] or priority == "Urgent"
    if needs_approval:
        conn.execute("""
            INSERT OR IGNORE INTO approvals
                (ticket_id, status, requested_by, requested_at)
            VALUES (?, 'pending', ?, ?)
        """, (ticket_id, ticket.get("employee_name", "unknown"),
              now.strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        log_workflow(conn, ticket_id, "approval_requested",
                     f"Approval required — category={category}, priority={priority}")

    # 3. Employee confirmation email
    if employee_email:
        subj = f"[Ticket #{ticket_id}] Submitted — {category} | {priority}"
        body = email_ticket_submitted(
            ticket_id, category, priority,
            ticket.get("ai_response", "Your request has been received."),
            ticket.get("employee_name", "Employee"),
            rule["department"]
        )
        sent   = _send_email(employee_email, subj, body)
        status = "sent" if sent else "simulated"
        save_notification(conn, ticket_id, employee_email, subj, body, status)
        log_workflow(conn, ticket_id, "notification_sent",
                     f"Confirmation email → {employee_email} [{status}]")

    # 4. Admin approval email
    if needs_approval:
        admin_email = os.environ.get("ADMIN_EMAIL", "admin@company.com")
        subj = f"[ACTION REQUIRED] Ticket #{ticket_id} Needs Approval — {priority}"
        body = email_approval_required(
            ticket_id, category, priority,
            ticket.get("ticket_text", ""),
            ticket.get("employee_name", "Employee"),
            base_url
        )
        sent   = _send_email(admin_email, subj, body)
        status = "sent" if sent else "simulated"
        save_notification(conn, ticket_id, admin_email, subj, body, status)
        log_workflow(conn, ticket_id, "approval_notification",
                     f"Approval request email → {admin_email} [{status}]")

    # 5. Webhook
    _fire_webhook(conn, "ticket_submitted", {
        "ticket_id": ticket_id, "category": category,
        "priority": priority, "department": rule["department"],
        "sla_due": sla_due,
    })

    return {"sla_due": sla_due, "needs_approval": needs_approval, "rule": rule}


def run_status_workflow(conn, ticket_id: int, new_status: str,
                        employee_email: str = None,
                        employee_name: str = "Employee",
                        category: str = "",
                        note: str = ""):
    log_workflow(conn, ticket_id, "status_changed",
                 f"Status → {new_status}" + (f" | Note: {note}" if note else ""))
    _fire_webhook(conn, "status_changed", {
        "ticket_id": ticket_id, "new_status": new_status,
        "category": category, "note": note,
    })
    if employee_email:
        subj = f"[Ticket #{ticket_id}] Status Updated → {new_status}"
        body = email_status_update(ticket_id, new_status, category, employee_name, note)
        sent   = _send_email(employee_email, subj, body)
        status = "sent" if sent else "simulated"
        save_notification(conn, ticket_id, employee_email, subj, body, status)


def run_approval_workflow(conn, ticket_id: int, decision: str,
                          reviewer: str,
                          employee_email: str = None,
                          employee_name: str = "Employee",
                          category: str = "",
                          note: str = ""):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""
        UPDATE approvals
        SET status=?, reviewed_by=?, review_note=?, reviewed_at=?
        WHERE ticket_id=?
    """, (decision, reviewer, note, now, ticket_id))
    new_status = "In Progress" if decision == "approved" else "Open"
    conn.execute("UPDATE tickets SET status=? WHERE id=?", (new_status, ticket_id))
    conn.commit()
    log_workflow(conn, ticket_id, f"approval_{decision}",
                 f"Reviewed by {reviewer}" + (f" — {note}" if note else ""))
    _fire_webhook(conn, "approval_decision", {
        "ticket_id": ticket_id, "decision": decision,
        "reviewer": reviewer, "note": note,
    })
    if employee_email:
        subj = f"[Ticket #{ticket_id}] {'Approved ✓' if decision=='approved' else 'Review Required'}"
        body = email_approval_decision(ticket_id, decision, category,
                                       employee_name, reviewer, note)
        sent   = _send_email(employee_email, subj, body)
        status = "sent" if sent else "simulated"
        save_notification(conn, ticket_id, employee_email, subj, body, status)


# ═══════════════════════════════════════════════════════════════════
# BULK ACTIONS
# ═══════════════════════════════════════════════════════════════════

def bulk_approve(conn, ticket_ids: list, reviewer: str, note: str = "Bulk approved") -> int:
    """Approve multiple tickets at once. Returns count processed."""
    count = 0
    for tid in ticket_ids:
        ticket = conn.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
        if not ticket:
            continue
        approval = conn.execute("SELECT * FROM approvals WHERE ticket_id=?", (tid,)).fetchone()
        if approval and approval["status"] == "pending":
            emp = conn.execute("SELECT email FROM users WHERE id=?",
                               (ticket["user_id"],)).fetchone()
            run_approval_workflow(
                conn, tid, "approved", reviewer,
                employee_email=emp["email"] if emp else None,
                employee_name=ticket["employee_name"],
                category=ticket["category"],
                note=note
            )
            count += 1
    return count


def bulk_reject(conn, ticket_ids: list, reviewer: str, note: str = "Bulk rejected") -> int:
    count = 0
    for tid in ticket_ids:
        ticket = conn.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
        if not ticket:
            continue
        approval = conn.execute("SELECT * FROM approvals WHERE ticket_id=?", (tid,)).fetchone()
        if approval and approval["status"] == "pending":
            emp = conn.execute("SELECT email FROM users WHERE id=?",
                               (ticket["user_id"],)).fetchone()
            run_approval_workflow(
                conn, tid, "rejected", reviewer,
                employee_email=emp["email"] if emp else None,
                employee_name=ticket["employee_name"],
                category=ticket["category"],
                note=note
            )
            count += 1
    return count


# ═══════════════════════════════════════════════════════════════════
# ESCALATION ENGINE
# ═══════════════════════════════════════════════════════════════════

def run_escalation_check(conn, base_url: str = "http://localhost:5000") -> list:
    """
    Scan all open/in-progress tickets for SLA breaches.
    For breached tickets not yet escalated: send alert email + log.
    Returns list of escalated ticket dicts.
    """
    init_workflow_tables(conn)
    now     = datetime.now()
    tickets = conn.execute("""
        SELECT t.*, u.email as user_email
        FROM tickets t
        LEFT JOIN users u ON t.user_id = u.id
        WHERE t.status IN ('Open','In Progress') AND t.sla_due IS NOT NULL
    """).fetchall()

    escalated = []
    for t in tickets:
        try:
            due = datetime.strptime(t["sla_due"], "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        if due >= now:
            continue  # not breached yet

        hours_overdue = (now - due).total_seconds() / 3600

        # Already escalated?
        existing = conn.execute(
            "SELECT id FROM escalations WHERE ticket_id=?", (t["id"],)
        ).fetchone()
        if existing:
            continue

        # Log escalation
        conn.execute("""
            INSERT OR IGNORE INTO escalations (ticket_id, escalated_at, reason, notified)
            VALUES (?, ?, ?, 1)
        """, (t["id"], now.strftime("%Y-%m-%d %H:%M:%S"),
              f"SLA breached by {hours_overdue:.1f}h"))
        conn.execute("UPDATE tickets SET escalated=1 WHERE id=?", (t["id"],))
        conn.commit()

        log_workflow(conn, t["id"], "sla_breached",
                     f"SLA breached by {hours_overdue:.1f}h — auto-escalated")

        # Email admin
        admin_email = os.environ.get("ADMIN_EMAIL", "admin@company.com")
        subj = f"🚨 SLA BREACH — Ticket #{t['id']} ({t['category']}) overdue by {hours_overdue:.0f}h"
        body = email_escalation_alert(
            t["id"], t["category"],
            t["priority"] or "Normal",
            t["employee_name"], hours_overdue, base_url
        )
        sent   = _send_email(admin_email, subj, body)
        status = "sent" if sent else "simulated"
        save_notification(conn, t["id"], admin_email, subj, body, status)

        _fire_webhook(conn, "sla_breached", {
            "ticket_id": t["id"], "category": t["category"],
            "hours_overdue": round(hours_overdue, 1),
            "priority": t["priority"] or "Normal",
        })

        escalated.append({
            "ticket_id":     t["id"],
            "category":      t["category"],
            "hours_overdue": round(hours_overdue, 1),
        })

    return escalated


# ═══════════════════════════════════════════════════════════════════
# SLA HELPER
# ═══════════════════════════════════════════════════════════════════

def get_sla_status(sla_due_str: str) -> dict:
    if not sla_due_str:
        return {"sla_label": "No SLA", "sla_class": "sla-none"}
    try:
        due  = datetime.strptime(sla_due_str, "%Y-%m-%d %H:%M:%S")
        now  = datetime.now()
        diff = due - now
        hrs  = diff.total_seconds() / 3600
        if hrs < 0:
            return {"sla_label": f"Breached {abs(int(hrs))}h ago", "sla_class": "sla-breach"}
        elif hrs <= 4:
            return {"sla_label": f"Due in {int(hrs)}h", "sla_class": "sla-warn"}
        else:
            return {"sla_label": f"Due {due.strftime('%d %b %H:%M')}", "sla_class": "sla-ok"}
    except Exception:
        return {"sla_label": "—", "sla_class": "sla-none"}


# ═══════════════════════════════════════════════════════════════════
# WORKFLOW RULE CRUD
# ═══════════════════════════════════════════════════════════════════

def create_workflow_rule(conn, name, category, department,
                         sla_hours, requires_approval, escalate_after_hrs):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""
        INSERT INTO workflow_rules
            (name, category, department, sla_hours, requires_approval,
             escalate_after_hrs, is_active, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
    """, (name, category, department, sla_hours,
          1 if requires_approval else 0, escalate_after_hrs, now, now))
    conn.commit()
    # Sync back into ROUTING_RULES so it takes effect immediately
    ROUTING_RULES[category] = {
        "department": department, "sla_hours": sla_hours,
        "requires_approval": bool(requires_approval),
        "approval_threshold": "Normal" if requires_approval else "Urgent",
        "escalate_after_hrs": escalate_after_hrs,
        "notify_roles": ["admin"], "icon": "🔧",
    }


def update_workflow_rule(conn, rule_id, sla_hours, requires_approval, escalate_after_hrs):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""
        UPDATE workflow_rules
        SET sla_hours=?, requires_approval=?, escalate_after_hrs=?, updated_at=?
        WHERE id=?
    """, (sla_hours, 1 if requires_approval else 0, escalate_after_hrs, now, rule_id))
    conn.commit()
    # Re-sync
    row = conn.execute("SELECT * FROM workflow_rules WHERE id=?", (rule_id,)).fetchone()
    if row:
        ROUTING_RULES[row["category"]] = {
            "department": row["department"], "sla_hours": row["sla_hours"],
            "requires_approval": bool(row["requires_approval"]),
            "approval_threshold": "Normal" if row["requires_approval"] else "Urgent",
            "escalate_after_hrs": row["escalate_after_hrs"],
            "notify_roles": ["admin"], "icon": "🔧",
        }


def toggle_workflow_rule(conn, rule_id, active: bool):
    conn.execute("UPDATE workflow_rules SET is_active=? WHERE id=?",
                 (1 if active else 0, rule_id))
    conn.commit()
