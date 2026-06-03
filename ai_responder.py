"""
ai_responder.py
---------------
Generates category-aware, formal and friendly auto-responses
for support tickets using the Anthropic Claude API.
"""

import os
from groq import Groq

# Category-specific context so Claude tailors the response appropriately
CATEGORY_CONTEXT = {
    "IT": {
        "team": "IT Support Team",
        "emoji": "💻",
        "focus": "technical troubleshooting, hardware, software, connectivity, and system access issues",
        "next_steps_hint": "our technicians will review the issue and may remotely access your device or schedule an on-site visit",
    },
    "HR": {
        "team": "Human Resources Team",
        "emoji": "👥",
        "focus": "employee relations, policies, benefits, onboarding, leave, payroll, and workplace concerns",
        "next_steps_hint": "an HR representative will reach out to discuss this matter confidentially",
    },
    "Finance": {
        "team": "Finance Department",
        "emoji": "💰",
        "focus": "payments, reimbursements, budgets, invoices, and financial queries",
        "next_steps_hint": "our finance team will verify the details and process accordingly within the standard business timeline",
    },
    "Operations": {
        "team": "Operations Team",
        "emoji": "⚙️",
        "focus": "facilities, logistics, processes, equipment, and day-to-day operational matters",
        "next_steps_hint": "our operations staff will assess the situation and coordinate the necessary resources",
    },
}

# Fallback for unknown categories
DEFAULT_CONTEXT = {
    "team": "Support Team",
    "emoji": "🎫",
    "focus": "general support requests",
    "next_steps_hint": "our team will review your request and follow up shortly",
}


def generate_ticket_response(ticket_text: str, category: str) -> str:
    """
    Generate a formal, friendly auto-response for a submitted ticket.

    Args:
        ticket_text: The raw text of the submitted ticket.
        category:    The AI-classified category (IT, HR, Finance, Operations).

    Returns:
        A polished response string, or a safe fallback message on error.
    """
    ctx = CATEGORY_CONTEXT.get(category, DEFAULT_CONTEXT)

    system_prompt = f"""You are a professional customer-support assistant for the {ctx['team']}.

Your role is to write AUTO-RESPONSES to newly submitted support tickets.

TONE RULES — follow these strictly:
1. Always formal — no slang, contractions like "gonna" / "wanna", or casual phrasing.
2. Always friendly and empathetic — the user should feel heard and valued.
3. Never robotic or copy-paste generic — personalise each response to the specific issue described.
4. Keep it concise: 3-5 sentences maximum.
5. Do NOT resolve the issue or give technical instructions — this is an acknowledgement, not a solution.
6. Do NOT start with "Dear" or end with a long signature block — just the response body.

STRUCTURE of every response:
- Sentence 1: Acknowledge receipt and briefly reference the specific issue.
- Sentence 2: Affirm the ticket has been routed to the correct team ({ctx['team']}).
- Sentence 3: Explain the next step ({ctx['next_steps_hint']}).
- Sentence 4 (optional): Offer reassurance or set expectations on timing.

CATEGORY CONTEXT:
This ticket falls under {category} — {ctx['focus']}.

OUTPUT: Plain text only. No markdown, no bullet points, no headers.
"""

    user_prompt = f"New ticket submitted:\n\n\"{ticket_text}\"\n\nWrite the auto-response now."

    try:
        client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
        chat_completion = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=300,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        return chat_completion.choices[0].message.content.strip()

    except Exception as exc:
        # Graceful fallback — never crash ticket submission
        print(f"[ai_responder] Error generating response: {exc}")
        team = ctx["team"]
        return (
            f"Thank you for submitting your ticket. Your request has been received and "
            f"assigned to the {team}. We will review the details and follow up with you "
            f"as soon as possible. We appreciate your patience."
        )
