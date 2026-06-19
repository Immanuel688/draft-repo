
import asyncio
import os
import json
#import time as time_mod
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

from langgraph.graph import StateGraph, END
#from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import interrupt
from langchain_openai import ChatOpenAI
from dotenv import load_dotenv

#Database
#from psycopg_pool import ConnectionPool
#from langgraph.checkpoint.postgres import PostgresSaver

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
# Shared resources
# ---------------------------------------------------------------------------
from openai import OpenAI

openai_api_key       = os.getenv("OPENAI_API_KEY")
client               = OpenAI(api_key=openai_api_key)
llm                  = ChatOpenAI(model="gpt-4o-mini", temperature=0)
credentials_sheets   = get_google_credentials()
credentials_calendar = get_calendar_credentials()
DATABASE_URL         = os.getenv("DATABASE_URL")

# URL of the FastAPI server (same process in combined setup)
#FASTAPI_BASE_URL = os.getenv("FASTAPI_BASE_URL", "http://localhost:8000")

# ---------------------------------------------------------------------------
# Node 1 — collect input
# ---------------------------------------------------------------------------
async def node_collect_input(state: IncidentState) -> dict:
    ticket_id = state.get("ticket_id", "").strip()
    priority  = state.get("priority",  "").strip()
    subject   = state.get("subject",   "").strip()
    context   = state.get("context",   "").strip()

    print(f"\n✅ Input received — {ticket_id} | {priority} | {subject}")

    return {
        "ticket_id": ticket_id,
        "priority":  priority,
        "subject":   subject,
        "context":   context,
    }


# ---------------------------------------------------------------------------
# Node 2 — RAG lookup
# ---------------------------------------------------------------------------
async def node_rag_lookup(state: IncidentState) -> dict:
    print("\n🔍 RAG node: fetching teams from Google Sheets...")

    try:
        teams = await asyncio.to_thread(fetch_teams,credentials_sheets)
        cim   = await asyncio.to_thread(fetch_current_cim,credentials_sheets)
        fixed = await asyncio.to_thread(fetch_fixed_members,credentials_sheets)
    except Exception as e:
        print(f"⚠️  Sheets fetch failed: {e}")
        teams = []
        cim   = {
            "name":  os.getenv("FALLBACK_CIM_NAME",  "On-Call CIM"),
            "phone": os.getenv("FALLBACK_CIM_PHONE", "+00000000000"),
            "email": os.getenv("FALLBACK_CIM_EMAIL", ""),
        }
        fixed = {
            "stakeholder_dl": os.getenv("FALLBACK_STAKEHOLDER_DL", ""),
            "optional":       [],
        }

    sanitised_teams = [
        {"name": t["display_name"], "keywords_hint": t["keywords_hint"]}
        for t in teams
    ]

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

        response    = await llm.ainvoke(prompt)
        raw         = response.content.strip()
        raw         = raw.replace("```json", "").replace("```", "").strip()

        try:
            matched_ids = json.loads(raw)
        except json.JSONDecodeError:
            print(f"⚠️  LLM returned unexpected format: {raw}")
            matched_ids = []
    else:
        matched_ids = []

    tech_dls        = [t["dl_email"] for t in teams if t["display_name"] in matched_ids]
    required_emails = [fixed["stakeholder_dl"]] + tech_dls + [cim["email"]]

    print(f"   Matched teams   : {matched_ids}")
    print(f"   Required emails : {required_emails}")

    return {
        "recipient_emails": required_emails,
        "cim":              cim,
        "fixed":            fixed,
    }


# ---------------------------------------------------------------------------
# Node 3 — draft invite
# ---------------------------------------------------------------------------
async def node_draft_invite(state: IncidentState) -> dict:
    print("\n📝 Draft node: generating invite...")

    now        = datetime.now(timezone.utc)
    start_time = now + timedelta(minutes=15)
    end_time   = start_time + timedelta(hours=1)

    fixed = state.get("fixed", {"stakeholder_dl": "", "optional": []})

    draft = {
        "priority":           state["priority"],
        "ticket_id":          state["ticket_id"],
        "subject":            state["subject"],
        "title":              f"[{state['priority']}] Bridge Call — {state['ticket_id']} : {state['subject']}",
        "description":        f"Scheduling bridge call for troubleshooting of {state['subject']}.",
        "start_time":         start_time.isoformat(),
        "end_time":           end_time.isoformat(),
        "required_attendees": state["recipient_emails"],
        "optional_attendees": fixed["optional"],
        "stakeholder_dl":     fixed["stakeholder_dl"],
    }

    print(f"   Draft title : {draft['title']}")
    return {"draft_invite": draft}


# ---------------------------------------------------------------------------
# Node 4 — human approval (interrupt point)
# ---------------------------------------------------------------------------
async def node_human_approval(state: IncidentState) -> dict:
    """
    Pauses via interrupt() — frontend renders approval form.
    Resumes when agent submits the form via POST /approve/{thread_id}.

    Resume payload:
        {"action": "A"}                         — approve as-is
        {"action": "E",
         "subject":             "...",
         "priority":            "...",
         "tech_dls":            ["a@x.com"],
         "optional_attendees":  ["b@x.com"]}
    """
    draft          = state.get("approved_invite") or state["draft_invite"]
    stakeholder_dl = draft.get("stakeholder_dl", "")

    human_response = interrupt({
        "message": "Review and approve the bridge call invite",
        "draft":   draft,
    })

    action = human_response.get("action", "A").upper()

    if action == "E":
        new_subject  = human_response.get("subject")
        new_priority = human_response.get("priority")
        new_tech_dls = human_response.get("tech_dls")
        new_optional = human_response.get("optional_attendees")

        if new_subject:
            draft["subject"]  = new_subject
        if new_priority:
            draft["priority"] = new_priority
        if new_tech_dls:
            draft["required_attendees"] = [stakeholder_dl] + new_tech_dls
        if new_optional:
            draft["optional_attendees"] = new_optional

        draft["title"] = (
            f"[{draft['priority']}] Bridge Call — "
            f"{draft['ticket_id']} : {draft['subject']}"
        )

    # Stakeholder DL must never be removed
    if stakeholder_dl and stakeholder_dl not in draft["required_attendees"]:
        print("⚠️  Stakeholder DL was missing — re-adding automatically.")
        draft["required_attendees"].insert(0, stakeholder_dl)

    print(f"\n✅ Invite approved.")
    return {"approved_invite": draft}


# ---------------------------------------------------------------------------
# Node 5 — send calendar invite
# ---------------------------------------------------------------------------
async def node_send_invite(state: IncidentState) -> dict:
    print("\n📅 Calendar node: sending invite...")

    invite = state["approved_invite"]

    try:
        meet_link = await asyncio.to_thread(send_calendar_invite,invite, credentials_calendar)
    except Exception as e:
        print(f"⚠️  Calendar API error: {e}")
        meet_link = "https://meet.google.com/error-check-logs"

    print(f"\n✅ Calendar invite sent! Meet link: {meet_link}")
    return {"meet_link": meet_link}



# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------
async def build_graph(checkpointer):
    graph = StateGraph(IncidentState)

    graph.add_node("collect_input",  node_collect_input)
    graph.add_node("rag_lookup",     node_rag_lookup)
    graph.add_node("draft_invite",   node_draft_invite)
    graph.add_node("human_approval", node_human_approval)
    graph.add_node("send_invite",    node_send_invite)
    

    graph.set_entry_point("collect_input")
    graph.add_edge("collect_input",  "rag_lookup")
    graph.add_edge("rag_lookup",     "draft_invite")
    graph.add_edge("draft_invite",   "human_approval")
    graph.add_edge("human_approval", "send_invite")
    graph.add_edge("send_invite",    END)

    #return graph.compile(checkpointer=MemorySaver())

     #Build pool once at module load — reused across all requests
    #connection_pool = ConnectionPool(
    #    conninfo=DATABASE_URL,
    #    max_size=10,
    #    kwargs={"autocommit": True},
    #)
    #checkpointer = PostgresSaver(connection_pool)
    #checkpointer.setup()   # creates LangGraph's own internal tables — safe to call every startup

    return graph.compile(checkpointer=checkpointer)


# 1. Create an async wrapper function to handle the async operations
#async def main():
#    print("🚀 Building and compiling LangGraph...")

    # 2. Await the build_graph function to get the actual compiled graph object
    compiled_graph = await build_graph()

    print("🏃 Invoking graph with initial state...")

    # 3. Define your thread configuration (Required for MemorySaver/checkpointers)
    config = {"configurable": {"thread_id": "incident_session_1"}}

    # 4. Await the graph execution using .ainvoke
    response = await compiled_graph.ainvoke(
        {
            "ticket_id": "INC-45678",
            "priority": "P1",
            "subject": "pLANT NETWORK DOWN",
            "context": "PRODUCTION IS DOWN IN HOLDREGE PLANT",
        },
        config=config,  # Pass the config thread id here
    )

    print("\n✅ Graph Execution Complete!")
    print("Final State Output:", response)


#if __name__ == "__main__":
    # 5. Use asyncio.run to safely kick off the async event loop from the terminal
    asyncio.run(main())