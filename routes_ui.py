"""
routes_ui.py
------------
FastAPI router for all agent-facing UI screens.
 
Routes
------
  GET  /                     → Screen 1: incident input form
  POST /submit               → kick off LangGraph, redirect to approval
  GET  /approval/{thread_id} → Screen 2: draft invite review form
  POST /approve/{thread_id}  → resume graph after agent edits, redirect to results
  GET  /results/{thread_id}  → Screen 3: Meet link + CIM ETA panel
"""


import asyncio
import uuid
import app_state

from dotenv import load_dotenv
from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi import BackgroundTasks
from langgraph.types import Command
from pydantic import BaseModel

from call_utils import trigger_cim_call

router=APIRouter()
templates  = Jinja2Templates(directory="templates")

# iNPUT FORM
@router.get("/",response_class=HTMLResponse)
async def input_form(request:Request):
    """screen 1 - incident input form"""
    return templates.TemplateResponse(request=request, name="input_form.html")

# POST /submit — start graph, pause at human_approval, redirect to approval
@router.post("/submit")
async def submit_incident(request: Request):
    """
        Receives the form POST from Screen 1.
        Runs the graph until the interrupt() in node_human_approval, then redirects.
        Uses ainvoke so the event loop is never blocked.
        """
    form = await request.form()
    thread_id =str(uuid.uuid4())

    initial_state = {
        "ticket_id" : form.get("ticket_id","").strip(),
        "priority":  form.get("priority",  "").strip(),
        "subject":   form.get("subject",   "").strip(),
        "context":   form.get("context",   "").strip(),
    }
    
    config= {"configurable":{"thread_id": thread_id}}
      # imported here to avoid circular imports
    await app_state.bridge_graph.ainvoke(initial_state,config)

    return RedirectResponse(f"/approval/{thread_id}", status_code=303)

# Screen 2 — approval form
@router.get("/approval/{thread_id}", response_class=HTMLResponse)
async def approval_page(request: Request, thread_id: str):
    """
    Reads the paused graph state to extract the draft invite, then
    renders the approval form for the agent to review / edit.
    """
    config= {"configurable":{"thread_id":thread_id}}

    state= await app_state.bridge_graph.aget_state(config)
    # The interrupt payload lives in state.tasks[0].interrupts
    draft = None
    if state.tasks:
        for task in state.tasks:
            if hasattr(task, "interrupts") and task.interrupts:
                draft = task.interrupts[0].value.get("draft")
                break
 
    if not draft:
        draft = state.values.get("draft_invite", {})
 
    return templates.TemplateResponse(
        request=request,
        name="approval.html",
        context={"thread_id": thread_id, "draft": draft},
    )

# POST /approve — resume graph, trigger CIM call in background, redirect
@router.post("/approve/{thread_id}")
async def approve_invite(
    request: Request,
    thread_id: str,
    background_tasks: BackgroundTasks):
    """
    Receives the approval form POST.
    Resumes the paused graph with the agent's edits (or a plain approve).
    Sends the calendar invite (inside graph), then triggers the CIM call
    as a background task so the redirect is instant.
    """
 
    form   = await request.form()
    action = form.get("action", "A")
 
    resume_payload: dict = {"action": action}
 
    if action == "E":
        if subject  := form.get("subject", "").strip():
            resume_payload["subject"]  = subject
        if priority := form.get("priority", "").strip():
            resume_payload["priority"] = priority
        if required := form.get("required_attendees", "").strip():
            resume_payload["tech_dls"] = [e.strip() for e in required.split(",") if e.strip()]
        if optional := form.get("optional_attendees", "").strip():
            resume_payload["optional_attendees"] = [e.strip() for e in optional.split(",") if e.strip()]
 
    config = {"configurable": {"thread_id": thread_id}}
 
    # Resume: runs send_invite → END
    await app_state.bridge_graph.ainvoke(Command(resume=resume_payload), config=config)
 
    # Pull final state to get cim + invite for the call trigger
    state  = await app_state.bridge_graph.aget_state(config)
    values = state.values
    cim    = values.get("cim", {})
    invite = values.get("approved_invite", {})
 
    background_tasks.add_task(trigger_cim_call, thread_id, cim, invite)
 
    return RedirectResponse(f"/results/{thread_id}", status_code=303)

@router.get("/results/{thread_id}",response_class=HTMLResponse)
async def results_page(request:Request, thread_id:str):
    """
    Shows Meet link immediately.
    CIM ETA panel polls /thread-eta/{thread_id} via JS.
    """
 
    config = {"configurable": {"thread_id": thread_id}}
    state  = await app_state.bridge_graph.aget_state(config)
    values = state.values

    meet_link = values.get("meet_link", "")
    invite    = values.get("approved_invite", {})
    cim       = values.get("cim", {})

 
    return templates.TemplateResponse(
        request=request,
        name="results.html",
        context={
            "thread_id": thread_id,
            "meet_link": meet_link,
            "invite":    invite,
            "cim":       cim
        }
    )
    