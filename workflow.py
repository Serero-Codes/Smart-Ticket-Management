"""
workflow.py
-----------
End-to-End Workflow Automation Engine for Smart Ticket Management System.

Handles:
  - Auto-routing rules (category → department + SLA)
  - Approval workflow triggers (urgent tickets, high-value categories)
  - Email notification dispatch (SMTP with graceful fallback)
  - Notification log (persisted to DB)
  - Workflow event pipeline (called on ticket submit + status change)
"""

import os
import smtplib
import threading
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart


# ═══════════════════════════════════════════════════════════════════
# ROUTING RULES
# Each rule: category → { department, sla_hours, requires_approval,
#                         approval_threshold (priority), escalate_after_hours }
# ═══════════════════════════════════════════════════════════════════

ROUTING_RULES = {
    "IT": {
        "department":         "IT",
        "sla_hours":          8,
        "requires_approval":  False,
        "approval_threshold": "Urgent",   # only urgent IT needs approval
        "escalate_after_hrs": 24,
        "notify_roles":       ["admin"],
        "icon":               "💻",
    },
    "HR": {
        "department":         "HR",
        "sla_hours":          24,
        "requires_approval":  True,        # all HR tickets need approval
        "approval_threshold": "Normal",
        "escalate_after_hrs": 48,
        "notify_roles":       ["admin"],
        "icon":               "👥",
    },
    "Finance": {
        "department":         "Finance",
        "sla_hours":          48,
        "requires_approval":  True,        # finance always needs approval
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


# ═══════════════════════════════════════════════════════════════════
# DB HELPERS
# ═══════════════════════════════════════════════════════════════════

def init_workflow_tables(conn):
    """Create all workflow-related tables."""
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
    # Add sla_due column to tickets if missing
    existing = [r[1] for r in conn.execute("PRAGMA table_info(tickets)").fetchall()]
    if "sla_due" not in existing:
        conn.execute("ALTER TABLE tickets ADD COLUMN sla_due TEXT")
    if "workflow_status" not in existing:
        conn.execute("ALTER TABLE tickets ADD COLUMN workflow_status TEXT DEFAULT 'active'")
    conn.commit()


def log_workflow(conn, ticket_id, event, detail=""):
    conn.execute("""
        INSERT INTO workflow_log (ticket_id, event, detail, created_at)
        VALUES (?, ?, ?, ?)
    """, (ticket_id, event, detail, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()


def save_notification(conn, ticket_id, recipient, subject, body, status="sent"):
    conn.execute("""
        INSERT INTO notifications (ticket_id, recipient, subject, body, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (ticket_id, recipient, subject, body,
          status, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()


# ═══════════════════════════════════════════════════════════════════
# EMAIL
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
    """Send a single HTML email. Returns True on success."""
    cfg = EMAIL_CONFIG
    if not cfg["username"] or not cfg["password"]:
        # No SMTP config — silent no-op (logged as 'simulated')
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"{cfg['from_name']} <{cfg['from_addr']}>"
        msg["To"]      = to
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=10) as server:
            server.ehlo()
            server.starttls()
            server.login(cfg["username"], cfg["password"])
            server.sendmail(cfg["from_addr"], to, msg.as_string())
        return True
    except Exception as exc:
        print(f"[workflow] Email error → {exc}")
        return False


def _send_async(to, subject, html_body):
    """Fire-and-forget email in background thread."""
    t = threading.Thread(target=_send_email, args=(to, subject, html_body), daemon=True)
    t.start()


# ═══════════════════════════════════════════════════════════════════
# EMAIL TEMPLATES
# ═══════════════════════════════════════════════════════════════════

def _base_email(title: str, content: str, cta_label: str = "", cta_url: str = "") -> str:
    cta_html = ""
    if cta_label and cta_url:
        cta_html = f"""
        <div style="text-align:center;margin:28px 0 8px">
            <a href="{cta_url}" style="
                display:inline-block;padding:12px 28px;
                background:#2563eb;color:#fff;
                font-weight:700;font-size:14px;
                border-radius:8px;text-decoration:none;
                box-shadow:0 2px 8px rgba(37,99,235,0.35)">
                {cta_label}
            </a>
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
          This is an automated message from Smart Ticket Management System.<br>
          Please do not reply to this email.
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


def email_approval_required(ticket_id, category, priority, ticket_text, requested_by, base_url="http://localhost:5000"):
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
    return _base_email(
        "⚠️ Approval Required",
        content,
        cta_label="Review & Approve →",
        cta_url=f"{base_url}/approvals"
    )


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
    colour  = "#10b981" if decision == "approved" else "#ef4444"
    icon    = "✅" if decision == "approved" else "❌"
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


# ═══════════════════════════════════════════════════════════════════
# MAIN WORKFLOW PIPELINE
# Called on every ticket submission
# ═══════════════════════════════════════════════════════════════════

def run_ticket_workflow(conn, ticket_id: int, ticket: dict, employee_email: str = None, base_url: str = "http://localhost:5000"):
    """
    Full automation pipeline for a newly submitted ticket:
      1. Determine routing rule
      2. Set SLA due date on ticket
      3. Create approval request if required
      4. Send confirmation email to employee
      5. Send approval-required email to admin (if applicable)
      6. Log all events
    """
    category = ticket.get("category", "IT")
    priority = ticket.get("priority", "Normal")
    rule     = ROUTING_RULES.get(category, DEFAULT_RULE)
    now      = datetime.now()

    # ── 1. Set SLA due date ──
    sla_due = (now + timedelta(hours=rule["sla_hours"])).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("UPDATE tickets SET sla_due = ? WHERE id = ?", (sla_due, ticket_id))
    conn.commit()
    log_workflow(conn, ticket_id, "sla_set",
                 f"SLA due: {sla_due} ({rule['sla_hours']}h window)")

    # ── 2. Approval logic ──
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

    # ── 3. Email to employee (confirmation) ──
    if employee_email:
        subj = f"[Ticket #{ticket_id}] Submitted — {category} | {priority}"
        body = email_ticket_submitted(
            ticket_id, category, priority,
            ticket.get("ai_response", "Your request has been received."),
            ticket.get("employee_name", "Employee"),
            rule["department"]
        )
        sent = _send_email(employee_email, subj, body)
        status = "sent" if sent else "simulated"
        save_notification(conn, ticket_id, employee_email, subj, body, status)
        log_workflow(conn, ticket_id, "notification_sent",
                     f"Confirmation email → {employee_email} [{status}]")

    # ── 4. Email to admin if approval needed ──
    if needs_approval:
        admin_email = os.environ.get("ADMIN_EMAIL", "admin@company.com")
        subj = f"[ACTION REQUIRED] Ticket #{ticket_id} Needs Approval — {priority}"
        body = email_approval_required(
            ticket_id, category, priority,
            ticket.get("ticket_text", ""),
            ticket.get("employee_name", "Employee"),
            base_url
        )
        sent = _send_email(admin_email, subj, body)
        status = "sent" if sent else "simulated"
        save_notification(conn, ticket_id, admin_email, subj, body, status)
        log_workflow(conn, ticket_id, "approval_notification",
                     f"Approval request email → {admin_email} [{status}]")

    return {
        "sla_due":       sla_due,
        "needs_approval": needs_approval,
        "rule":          rule,
    }


def run_status_workflow(conn, ticket_id: int, new_status: str,
                        employee_email: str = None,
                        employee_name: str = "Employee",
                        category: str = "",
                        note: str = ""):
    """Called when a ticket status is updated."""
    log_workflow(conn, ticket_id, "status_changed",
                 f"Status → {new_status}" + (f" | Note: {note}" if note else ""))

    if employee_email:
        subj = f"[Ticket #{ticket_id}] Status Updated → {new_status}"
        body = email_status_update(ticket_id, new_status, category, employee_name, note)
        sent = _send_email(employee_email, subj, body)
        status = "sent" if sent else "simulated"
        save_notification(conn, ticket_id, employee_email, subj, body, status)


def run_approval_workflow(conn, ticket_id: int, decision: str,
                          reviewer: str,
                          employee_email: str = None,
                          employee_name: str = "Employee",
                          category: str = "",
                          note: str = ""):
    """Called when admin approves or rejects a ticket."""
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

    if employee_email:
        subj = f"[Ticket #{ticket_id}] {'Approved ✓' if decision=='approved' else 'Review Required'}"
        body = email_approval_decision(ticket_id, decision, category,
                                       employee_name, reviewer, note)
        sent = _send_email(employee_email, subj, body)
        status = "sent" if sent else "simulated"
        save_notification(conn, ticket_id, employee_email, subj, body, status)


# ═══════════════════════════════════════════════════════════════════
# SLA HELPER — check which tickets are breached or at risk
# ═══════════════════════════════════════════════════════════════════

def get_sla_status(sla_due_str: str) -> dict:
    """Returns sla_label and sla_class for template rendering."""
    if not sla_due_str:
        return {"sla_label": "No SLA", "sla_class": "sla-none"}
    try:
        due = datetime.strptime(sla_due_str, "%Y-%m-%d %H:%M:%S")
        now = datetime.now()
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
