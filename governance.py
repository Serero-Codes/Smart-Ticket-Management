"""
governance.py
-------------
Compliance & Risk Monitoring for Smart Ticket Management System.

Provides:
  - AI response bias/risk audit (checks for over-generalisation, dept skew, confidence drift)
  - Per-ticket transparency notes (why classified as X, confidence band)
  - Governance risk score across the platform
  - Audit log helpers
"""

from datetime import datetime
from collections import defaultdict


# ── Risk thresholds ───────────────────────────────────────────────────────────

LOW_CONFIDENCE_THRESHOLD  = 55.0   # below this = uncertain classification
HIGH_CONFIDENCE_THRESHOLD = 90.0
DEPT_IMBALANCE_RATIO      = 3.0    # one dept has 3× tickets of another → flag
URGENT_RATE_THRESHOLD     = 0.35   # >35% urgent = systemic stress flag


# ── Bias / risk flag definitions ─────────────────────────────────────────────

RISK_DEFINITIONS = {
    "low_confidence_classifications": {
        "label": "Low-Confidence Classifications",
        "description": (
            "Tickets classified with confidence below 55% may be misrouted, "
            "leading to incorrect department assignment and delayed resolution."
        ),
        "severity": "high",
        "mitigation": "Review flagged tickets manually. Retrain classifier with additional labelled examples.",
    },
    "dept_volume_imbalance": {
        "label": "Department Volume Imbalance",
        "description": (
            "One or more departments receive significantly more tickets than others. "
            "This may indicate uneven AI routing or a genuine operational bottleneck."
        ),
        "severity": "medium",
        "mitigation": "Audit routing logic and verify classifier performance per category.",
    },
    "high_urgent_rate": {
        "label": "Elevated Urgent Ticket Rate",
        "description": (
            "More than 35% of tickets are flagged as urgent. This may indicate "
            "over-triggering of urgency keywords or a real systemic service failure."
        ),
        "severity": "high",
        "mitigation": "Review urgency detection keywords and cross-check with actual SLA breaches.",
    },
    "ai_response_failures": {
        "label": "AI Response Generation Failures",
        "description": (
            "Some tickets received fallback template responses rather than AI-generated ones. "
            "This reduces response personalisation and user experience quality."
        ),
        "severity": "medium",
        "mitigation": "Check GROQ_API_KEY configuration and API quota limits.",
    },
    "single_category_dominance": {
        "label": "Single Category Dominance",
        "description": (
            "One ticket category accounts for over 60% of all tickets. "
            "This may indicate classifier bias toward a dominant training label."
        ),
        "severity": "medium",
        "mitigation": "Inspect training data balance. Augment under-represented categories.",
    },
    "confidence_drift": {
        "label": "Classifier Confidence Drift",
        "description": (
            "Average classifier confidence has dropped below 70%. "
            "This suggests the model may be encountering ticket types outside its training distribution."
        ),
        "severity": "high",
        "mitigation": "Collect misclassified samples and retrain the model periodically.",
    },
}


# ── Core audit function ───────────────────────────────────────────────────────

def run_governance_audit(tickets: list) -> dict:
    """
    Analyse all tickets and return a comprehensive governance report dict.
    """
    total = len(tickets)
    if total == 0:
        return _empty_audit()

    now = datetime.now().strftime("%d %B %Y, %H:%M")

    # ── Basic counts ──
    confidences   = []
    cat_counts    = defaultdict(int)
    dept_counts   = defaultdict(int)
    urgent_count  = 0
    low_conf_tix  = []
    fallback_count = 0
    fallback_phrase = "Thank you for submitting your ticket. Your request has been received"

    for t in tickets:
        conf = float(t["confidence"] or 0)
        confidences.append(conf)
        cat_counts[t["category"]] += 1
        dept = t["assigned_department"] or t["category"]
        dept_counts[dept] += 1

        if (t["priority"] or "Normal") == "Urgent":
            urgent_count += 1

        if conf < LOW_CONFIDENCE_THRESHOLD:
            low_conf_tix.append({
                "id":         t["id"],
                "text":       (t["ticket_text"] or "")[:80] + ("…" if len(t["ticket_text"] or "") > 80 else ""),
                "category":   t["category"],
                "confidence": round(conf, 1),
                "dept":       dept,
                "status":     t["status"],
            })

        ai_resp = t["ai_response"] or ""
        if fallback_phrase in ai_resp:
            fallback_count += 1

    avg_conf       = round(sum(confidences) / total, 1) if confidences else 0
    urgent_rate    = urgent_count / total if total else 0
    top_cat        = max(cat_counts, key=cat_counts.get) if cat_counts else "N/A"
    top_cat_share  = round(cat_counts[top_cat] / total * 100) if total else 0

    dept_vols = list(dept_counts.values())
    dept_ratio = (max(dept_vols) / min(dept_vols)) if len(dept_vols) > 1 and min(dept_vols) > 0 else 1.0

    # ── Identify active risks ──
    active_risks = []

    if low_conf_tix:
        r = dict(RISK_DEFINITIONS["low_confidence_classifications"])
        r["count"]  = len(low_conf_tix)
        r["detail"] = f"{len(low_conf_tix)} ticket{'s' if len(low_conf_tix) != 1 else ''} classified below 55% confidence."
        active_risks.append(r)

    if dept_ratio >= DEPT_IMBALANCE_RATIO:
        r = dict(RISK_DEFINITIONS["dept_volume_imbalance"])
        r["count"]  = None
        r["detail"] = f"Highest-volume dept has {round(dept_ratio, 1)}× more tickets than the lowest."
        active_risks.append(r)

    if urgent_rate >= URGENT_RATE_THRESHOLD:
        r = dict(RISK_DEFINITIONS["high_urgent_rate"])
        r["count"]  = urgent_count
        r["detail"] = f"{round(urgent_rate * 100)}% of tickets flagged urgent ({urgent_count} total)."
        active_risks.append(r)

    if fallback_count > 0:
        r = dict(RISK_DEFINITIONS["ai_response_failures"])
        r["count"]  = fallback_count
        r["detail"] = f"{fallback_count} ticket{'s' if fallback_count != 1 else ''} received fallback template responses."
        active_risks.append(r)

    if top_cat_share > 60:
        r = dict(RISK_DEFINITIONS["single_category_dominance"])
        r["count"]  = None
        r["detail"] = f"'{top_cat}' accounts for {top_cat_share}% of all tickets."
        active_risks.append(r)

    if avg_conf < 70:
        r = dict(RISK_DEFINITIONS["confidence_drift"])
        r["count"]  = None
        r["detail"] = f"Average classifier confidence is {avg_conf}% — below the 70% threshold."
        active_risks.append(r)

    # ── Overall risk score (0–100) ──
    severity_weights = {"high": 25, "medium": 12}
    raw_score = sum(severity_weights.get(r["severity"], 0) for r in active_risks)
    risk_score = min(100, raw_score)

    if risk_score >= 50:
        risk_level = "critical"
    elif risk_score >= 25:
        risk_level = "elevated"
    elif risk_score > 0:
        risk_level = "low"
    else:
        risk_level = "clear"

    # ── Confidence distribution bands ──
    bands = {"<55%": 0, "55–70%": 0, "70–85%": 0, "85–100%": 0}
    for c in confidences:
        if c < 55:
            bands["<55%"] += 1
        elif c < 70:
            bands["55–70%"] += 1
        elif c < 85:
            bands["70–85%"] += 1
        else:
            bands["85–100%"] += 1

    # ── Per-dept classification accuracy proxy ──
    dept_conf = defaultdict(list)
    for t in tickets:
        dept = t["assigned_department"] or t["category"]
        dept_conf[dept].append(float(t["confidence"] or 0))
    dept_accuracy = {
        d: round(sum(v) / len(v), 1)
        for d, v in dept_conf.items()
    }

    # ── Category distribution for chart ──
    cat_labels = list(cat_counts.keys())
    cat_values = [cat_counts[c] for c in cat_labels]

    # ── Transparency notes per low-conf ticket ──
    for tix in low_conf_tix:
        conf = tix["confidence"]
        if conf < 40:
            note = "Very uncertain — classifier had less than 40% confidence. Manual review strongly advised."
        elif conf < 55:
            note = f"Below confidence threshold ({conf}%). Ticket may have been misrouted."
        tix["transparency_note"] = note

    return {
        "generated_at":    now,
        "total":           total,
        "avg_confidence":  avg_conf,
        "urgent_count":    urgent_count,
        "urgent_rate_pct": round(urgent_rate * 100),
        "fallback_count":  fallback_count,
        "low_conf_count":  len(low_conf_tix),
        "low_conf_tickets": low_conf_tix[:20],
        "top_cat":         top_cat,
        "top_cat_share":   top_cat_share,
        "risk_score":      risk_score,
        "risk_level":      risk_level,
        "active_risks":    active_risks,
        "conf_band_labels": list(bands.keys()),
        "conf_band_values": list(bands.values()),
        "cat_labels":      cat_labels,
        "cat_values":      cat_values,
        "dept_accuracy":   dept_accuracy,
    }


def _empty_audit() -> dict:
    return {
        "generated_at": datetime.now().strftime("%d %B %Y, %H:%M"),
        "total": 0, "avg_confidence": 0, "urgent_count": 0,
        "urgent_rate_pct": 0, "fallback_count": 0, "low_conf_count": 0,
        "low_conf_tickets": [], "top_cat": "N/A", "top_cat_share": 0,
        "risk_score": 0, "risk_level": "clear", "active_risks": [],
        "conf_band_labels": ["<55%", "55–70%", "70–85%", "85–100%"],
        "conf_band_values": [0, 0, 0, 0],
        "cat_labels": [], "cat_values": [],
        "dept_accuracy": {},
    }


# ── Audit log helper ──────────────────────────────────────────────────────────

def log_governance_event(conn, event_type: str, detail: str, user: str = "system"):
    """Insert a row into the governance_log table."""
    try:
        conn.execute("""
            INSERT INTO governance_log (event_type, detail, triggered_by, created_at)
            VALUES (?, ?, ?, ?)
        """, (event_type, detail, user, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
    except Exception as e:
        print(f"[governance] Log error: {e}")


def init_governance_table(conn):
    """Create governance_log table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS governance_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type   TEXT NOT NULL,
            detail       TEXT,
            triggered_by TEXT DEFAULT 'system',
            created_at   TEXT NOT NULL
        )
    """)
    conn.commit()
