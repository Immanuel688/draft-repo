"""
graph.py
--------
LangGraph orchestrator for the Bridge Bot.

Flow:
  node_collect_input
       ↓
  node_rag_lookup        ← fetches from Sheets, LLM matches team IDs
       ↓
  node_draft_invite      ← LLM drafts title, description, timing
       ↓
  node_human_approval    ← PAUSES here for service desk agent review
       ↓                   (interrupt_before this node)
  node_send_invite       ← creates Google Calendar event
       ↓
  END
"""

import os
import json
from datetime import datetime, timedelta, timezone,time
from dotenv import load_dotenv

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from langchain_openai import ChatOpenAI

from state import IncidentState
from sheets import (
    fetch_teams,
    fetch_current_cim,
    fetch_fixed_members,
    get_google_credentials,
)
from calendar_tool import send_calendar_invite, get_calendar_credentials

load_dotenv()

# ---------------------------------------------------------------------------
# Shared resources (initialised once)
# ---------------------------------------------------------------------------
from openai import OpenAI
openai_api_key = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=openai_api_key)# needs to be passed as keyword argument or can be left empty as we fetcg from env variable

# ---------------------------------------------------------------------------
# Shared resources (initialised once)
# ---------------------------------------------------------------------------

llm         = ChatOpenAI(model="gpt-4o-mini", temperature=0)
credentials_sheets = get_google_credentials()
credentials_calendar= get_calendar_credentials()

# ---------------------------------------------------------------------------
# Node 1 — collect input
# ---------------------------------------------------------------------------

def node_collect_input(state: IncidentState) -> dict:
    """
    Validates that all required input fields are present.
    In a real UI this node would be skipped — the UI sends the filled state.
    For Jupyter / terminal usage it prompts interactively.
    """
    ticket_id = state.get("ticket_id", "").strip()
    priority  = state.get("priority",  "").strip()
    subject   = state.get("subject",   "").strip()
    context   = state.get("context",   "").strip()

    # If running interactively and fields are missing, prompt
    if not ticket_id:
        ticket_id = input("Ticket ID     : ").strip()
    if not priority:
        priority  = input("Priority (P1/P2): ").strip().upper()
    if not subject:
        subject   = input("Subject       : ").strip()
    if not context:
        context   = input("Context       : ").strip()

    print(f"\n✅ Input received — {ticket_id} | {priority} | {subject}")

    return {
        "ticket_id": ticket_id,
        "priority":  priority,
        "subject":   subject,
        "context":   context,
    }


# ---------------------------------------------------------------------------
# Node 2 — RAG lookup (Sheets fetch + LLM classification)
# ---------------------------------------------------------------------------

def node_rag_lookup(state: IncidentState) -> dict:
    """
    Step 1: Fetches ALL teams from Google Sheets.
    Step 2: Sends only sanitised {id, keywords_hint} to LLM — NO PII.
    Step 3: LLM returns matching team IDs.
    Step 4: Resolves actual DL emails locally — PII never leaves this node.
    Step 5: Fetches current on-call CIM from Sheets.
    """
    print("\n🔍 RAG node: fetching teams from Google Sheets...")

    # --- fetch from Sheets ---
    try:
        teams       = fetch_teams(credentials_sheets)
        cim         = fetch_current_cim(credentials_sheets)
        fixed       = fetch_fixed_members(credentials_sheets)
    except Exception as e:
        print(f"⚠️  Sheets fetch failed: {e}")
        print("   Falling back to environment variable defaults.")
        teams = []
        cim   = {
            "name":  os.getenv("FALLBACK_CIM_NAME",  "On-Call CIM"),
            "phone": os.getenv("FALLBACK_CIM_PHONE", "+00000000000"),
        }
        fixed = {
            "stakeholder_dl": os.getenv("FALLBACK_STAKEHOLDER_DL", ""),
            "optional":       [],
        }

    # --- sanitise — only hints go to LLM ---
    sanitised_teams = [
        {"name": t["display_name"], "keywords_hint": t["keywords_hint"]}
        for t in teams
    ]

    # --- LLM classification ---
    if sanitised_teams:
        prompt = f"""You are an IT incident routing assistant.

Incident subject : {state["subject"]}
Incident context : {state["context"]}

Available teams:
{json.dumps(sanitised_teams, indent=2)}

Return ONLY a JSON array of team names that should be paged for this incident.
Return an empty array [] if no team matches.
No explanation. No markdown. Only raw JSON array.
Example: ["network", "database"]"""

        response    = llm.invoke(prompt)
        raw         = response.content.strip()

        # strip markdown fences if model adds them
        raw         = raw.replace("```json", "").replace("```", "").strip()

        try:
            matched_ids = json.loads(raw)
        except json.JSONDecodeError:
            print(f"⚠️  LLM returned unexpected format: {raw}")
            matched_ids = []
    else:
        matched_ids = []

    # --- resolve DL emails locally ---
    tech_dls = [
        t["dl_email"] for t in teams if t["display_name"] in matched_ids
    ]

    # --- build full required list (stakeholder always first) ---
    required_emails = [fixed["stakeholder_dl"]] + tech_dls + [cim['email']]

    print(f"   Matched teams   : {matched_ids}")
    print(f"   Required emails : {required_emails}")
    print(f"   On-call CIM     : {cim['name']}")

    return {
        "recipient_emails": required_emails,
        "cim":              cim,
        # store fixed for use in draft node
        "fixed":           fixed,          # internal — not in TypedDict but
                                             # LangGraph passes extra keys safely
    }

# ---------------------------------------------------------------------------
# Node 3 — draft invite
# ---------------------------------------------------------------------------

def node_draft_invite(state: IncidentState) -> dict:
    """
    LLM drafts the invite CONTENT only (title, description, timing).
    Attendee list is assembled here in code — LLM never sees emails.
    """
    print("\n📝 Draft node: generating invite with LLM...")

    # timing — start 15 min from now, 1 hour duration
    now        = datetime.now(timezone.utc)
    start_time = now + timedelta(minutes=15)
    end_time   = start_time + timedelta(hours=1)

    content = {
            "title":       f"[{state['priority']}] Bridge Call — {state['ticket_id']} : {state['subject']}",
            "description": f"Scheduling bridge call for troubleshooting"
        }

    # retrieve fixed members stored by RAG node
    fixed = state.get("fixed", {"stakeholder_dl": "", "optional": []})

    draft = {
        "title":               content["title"],
        "description":         content["description"],
        "start_time":          start_time.isoformat(),
        "end_time":            end_time.isoformat(),
        "required_attendees":  state["recipient_emails"],   # stakeholder + tech DLs
        "optional_attendees":  fixed["optional"],
        "stakeholder_dl":      fixed["stakeholder_dl"],     # stored for HITL protection
        "ticket_id":           state["ticket_id"],
    }

    print(f"\n   Draft title : {draft['title']}")
    print(f"   Required    : {draft['required_attendees']}")
    print(f"   Optional    : {draft['optional_attendees']}")

    return {"draft_invite": draft}


# ---------------------------------------------------------------------------
# Node 4 — human approval (interrupt point)
# ---------------------------------------------------------------------------

def node_human_approval(state: IncidentState) -> dict:
    """
    This node is the INTERRUPT point.
    LangGraph pauses BEFORE entering this node.

    When the graph resumes (after human reviews in UI/notebook),
    this node validates the business rule:
    → Stakeholder DL must always be in required_attendees.

    The service desk agent can:
      - Approve as-is
      - Edit tech DLs
      - Edit optional list
    They CANNOT remove the stakeholder DL — enforced in code.
    """
    draft             = state.get("approved_invite") or state["draft_invite"]
    stakeholder_dl    = draft.get("stakeholder_dl", "")

    # -----------------------------------------------------------------------
    # INTERACTIVE approval for Jupyter / terminal
    # In a real UI, the frontend sends the approved/edited invite back
    # and the graph resumes — this block is replaced by a UI form.
    # -----------------------------------------------------------------------
    print("\n" + "="*60)
    print("🔔 HUMAN APPROVAL REQUIRED")
    print("="*60)
    print(f"Title       : {draft['title']}")
    print(f"Start       : {draft['start_time']}")
    print(f"\nDESCRIPTION:\n{draft['description']}")
    print(f"\nREQUIRED ATTENDEES:")
    print(f"  [PROTECTED] Stakeholder DL : {stakeholder_dl}")
    for email in draft["required_attendees"]:
        if email != stakeholder_dl:
            print(f"  Tech DL : {email}")
    print(f"\nOPTIONAL ATTENDEES:")
    for email in draft["optional_attendees"]:
        print(f"  {email}")
    print("="*60)

    action = input("\nAction — [A]pprove / [E]dit : ").strip().upper()

    if action == "E":
        print("\nCurrent tech DLs:", [
            e for e in draft["required_attendees"] if e != stakeholder_dl
        ])
        raw_tech = input(
            "Enter updated tech DLs (comma-separated, or press Enter to keep): "
        ).strip()

        if raw_tech:
            new_tech_dls = [e.strip() for e in raw_tech.split(",") if e.strip()]
            # BUSINESS RULE: stakeholder always stays, always first
            draft["required_attendees"] = [stakeholder_dl] + new_tech_dls

        raw_opt = input(
            "Update optional attendees? (comma-separated, or press Enter to keep): "
        ).strip()
        if raw_opt:
            draft["optional_attendees"] = [
                e.strip() for e in raw_opt.split(",") if e.strip()
            ]

    # --- final enforcement — belt and braces ---
    if stakeholder_dl and stakeholder_dl not in draft["required_attendees"]:
        print("⚠️  Stakeholder DL was missing — re-adding automatically.")
        draft["required_attendees"].insert(0, stakeholder_dl)

    print(f"\n✅ Invite approved.")
    return {"approved_invite": draft}


# ---------------------------------------------------------------------------
# Node 5 — send calendar invite
# ---------------------------------------------------------------------------

def node_send_invite(state: IncidentState) -> dict:
    """
    Final safety check then creates the Google Calendar event.
    Returns the Meet link into state.
    """
    print("\n📅 Calendar node: sending invite...")

    invite         = state["approved_invite"]
    stakeholder_dl = invite.get("stakeholder_dl", "")

    # --- final hard check before sending ---
#   if stakeholder_dl and stakeholder_dl not in invite["required_attendees"]:
#       raise ValueError(
#           "CRITICAL: Stakeholder DL missing from required attendees. "
#           "Blocking calendar send."
#       )

    try:
        meet_link = send_calendar_invite(invite, credentials_calendar)
    except Exception as e:
        print(f"⚠️  Calendar API error: {e}")
        meet_link = "https://meet.google.com/error-check-logs"

    print(f"\n✅ Calendar invite sent!")
    print(f"   Meet link: {meet_link}")

    return {"meet_link": meet_link}


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------

def build_graph():
    graph = StateGraph(IncidentState)

    graph.add_node("collect_input",    node_collect_input)
    graph.add_node("rag_lookup",       node_rag_lookup)
    graph.add_node("draft_invite",     node_draft_invite)
    graph.add_node("human_approval",   node_human_approval)
    graph.add_node("send_invite",      node_send_invite)

    graph.set_entry_point("collect_input")
    graph.add_edge("collect_input",  "rag_lookup")
    graph.add_edge("rag_lookup",     "draft_invite")
    graph.add_edge("draft_invite",   "human_approval")
    graph.add_edge("human_approval", "send_invite")
    graph.add_edge("send_invite",     END)

    return graph.compile(
        checkpointer=MemorySaver(),
        #interrupt_before=["human_approval"],   # ← pauses here for human review
    )


# module-level compiled graph
bridge_graph = build_graph()
