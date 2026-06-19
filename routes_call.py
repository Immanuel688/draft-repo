"""
routes_call.py
--------------
FastAPI router for everything related to the outbound CIM call.
 
Routes
------
  GET  /thread-eta/{thread_id}  → frontend polls for CIM ETA
  POST /internal/call           → trigger Twilio outbound call + write DB row
  POST /twiml                   → Twilio webhook: returns TwiML stream XML
  WS   /media-stream            → Twilio ↔ OpenAI Realtime bridge
 
Also exports
------------
  trigger_cim_call()  — async helper used by routes_ui.py as a background task
"""

import asyncio
import json
#import logging
import os
import re
 
import httpx
import websockets
from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import HTMLResponse
from fastapi.websockets import WebSocketDisconnect
from pydantic import BaseModel
from twilio.rest import Client as TwilioClient
 
from db import pool

from dotenv import load_dotenv

load_dotenv()
 
#log    = logging.getLogger(__name__)
router = APIRouter()

# ---------------------------------------------------------------------------
# Env vars
# ---------------------------------------------------------------------------
OPENAI_API_KEY        = os.getenv("OPENAI_API_KEY")
TWILIO_ACCOUNT_SID    = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN     = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER   = os.getenv("TWILIO_PHONE_NUMBER")
OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-mini-2025-12-15")
OPENAI_REALTIME_URL   = f"wss://api.openai.com/v1/realtime?model={OPENAI_REALTIME_MODEL}"
PUBLIC_BASE_URL       = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")  # ngrok or cloud URL

twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

class IncidentPayload(BaseModel):
    thread_id:   str
    cim_name:    str
    cim_phone:   str
    ticket_id:   str
    priority:    str
    description: str

def build_system_prompt(incident: dict) -> str:
    return f"""
You are an automated IT incident management assistant making an outbound
notification call to a Critical Incident Manager (CIM) named {incident['cim_name']}.
 
INCIDENT DETAILS (you already know these — do not ask):
  - Incident ID  : {incident['id']}
  - Priority     : {incident['priority']}
  - Description  : {incident['description']}
  - A bridge call invite has already been sent to their email
 
STRICT CONVERSATION SCRIPT — follow this exact sequence every call:
 
STEP 1 — GREETING (your first turn only):
  Say exactly: "Hello {incident['cim_name']}, this is the automated IT incident assistant. I'm calling about a critical incident. Is this a good time?"
  Then STOP and wait.
 
STEP 2 — INCIDENT ID + PRIORITY (after they acknowledge):
  Say exactly: "The incident ID is {incident['id']} and the priority is {incident['priority']}."
  Then STOP and wait.
 
STEP 3 — DESCRIPTION (after they respond):
  Say exactly: "The issue is: {incident['description']}."
  Then STOP and wait.
 
STEP 4 — BRIDGE CALL (after they respond):
  Say exactly: "A bridge call invite has been sent to your email. Can you please confirm your estimated time to join?"
  Then STOP and wait.
 
STEP 5 — CONFIRM ETA + CLOSE (after they give ETA):
  Repeat their ETA back and close the call: "Got it, <their ETA>. ETA_CONFIRMED: <their ETA>. Thank you {incident['cim_name']}, goodbye."
  Then, on a new line, write the silent marker exactly as: ETA_CONFIRMED:<their ETA>
  The marker is a text-only signal for the system.
  Then STOP — do not say anything further.
 
TONE AND STYLE RULES (apply to every turn):
  - Speak in a calm, professional,polite, concise tone — no filler words, no small talk
  - Keep each turn to 1-2 short sentences maximum
  - Never chain two topics in the same turn
  - Never talk over the CIM
  - Never deviate from the script above
  - If the CIM asks a question you cannot answer, say: "Unfortunately,  I don't have that details — please join the bridge call for the details."
""" 

# GET /thread-eta/{thread_id} — frontend polling endpoint
@router.get("/thread-eta/{thread_id}")
async def get_eta_by_thread(thread_id: str):
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT eta FROM incident_calls WHERE thread_id = %s", # %s is a place holder and the value thread_id is passed inside via tuple
            (thread_id,) # python psycopg library separates query and value
        )#.fetchone()
        row = await cur.fetchone() #cur is a cursor object, row is tuple, and returns one row
    eta = row[0] if row else None
    return {"ready": bool(eta), "eta": eta or ""}

# POST /internal/call — trigger Twilio call + persist incident row
@router.post("/internal/call")
async def internal_call(payload: IncidentPayload):
    """
    Triggers Twilio outbound call and returns call_sid immediately.
    ETA is captured asynchronously via the WebSocket bridge.
    """
    
    call = twilio_client.calls.create(
        to=payload.cim_phone,
        from_=TWILIO_PHONE_NUMBER,
        url=f"{PUBLIC_BASE_URL}/twiml",
    )

    # THIS QUERY CHECKS IF THREAD_ID IS PRESENT ID YES IT OVERWRITES IT AND APPENDS THE DETAILS TO THE RESPECTIVE ROWS
    # THESE VALUES ARE STORED INA PSEUDO OR TEMP TABLE CALLED EXCLUDED
    # ON CONFLICT HELPS US TO HANDLE PRIMARY KEY ERROR AND DO UPDATE helps to update values using SET
    async with pool.connection() as conn:
        await conn.execute(
            """INSERT INTO incident_calls
               (thread_id, call_sid, cim_name, ticket_id, priority, description)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (thread_id) DO UPDATE SET
               call_sid=EXCLUDED.call_sid,
               cim_name=EXCLUDED.cim_name,
               ticket_id=EXCLUDED.ticket_id,
               priority=EXCLUDED.priority,
               description=EXCLUDED.description""",
            (payload.thread_id, call.sid, payload.cim_name,
             payload.ticket_id, payload.priority, payload.description)
        )

    # Debug 4 — confirm row was written
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT call_sid FROM incident_calls WHERE call_sid = %s",
            (call.sid,)
        )#.fetchone()
        verify = await cur.fetchone()
    print(f"🔍 DB verify after write: {verify}")


    print(f"📞 Call triggered — SID: {call.sid}")
    return {"call_sid": call.sid, "status": "dialing"}

# POST /twiml — Twilio webhook: returns Media Stream XML
# ---------------------------------------------------------------------------
@router.post("/twiml")
async def twiml(request: Request):
    host = request.url.hostname
    xml  = f"""<?xml version="1.0" encoding="UTF-8"?>
    <Response>
        <Connect>
            <Stream url="wss://{host}/media-stream"/>
        </Connect>
    </Response>"""
    return HTMLResponse(content=xml, media_type="application/xml")

@router.websocket("/media-stream")
async def media_stream(twilio_ws: WebSocket ):
    """
    WebSocket bridge: Twilio ↔ OpenAI Realtime.
    Detects ETA_CONFIRMED sentinel and stores result in eta_store.
    """
    await twilio_ws.accept()

    async with websockets.connect(
        OPENAI_REALTIME_URL,
        additional_headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
    ) as openai_ws:

        stream_sid    = None
        call_sid     = None
        extracted_eta = None
        incident      = None 

        # ── Task 1: Twilio → OpenAI ──────────────────────────────────────
        async def receive_from_twilio():
            nonlocal stream_sid, call_sid, incident
            try:
                async for message in twilio_ws.iter_text():
                    data       = json.loads(message)
                    event_type = data.get("event")

                    if event_type == "start":
                        stream_sid = data["start"]["streamSid"]
                        call_sid   = data["start"].get("callSid")
                        print(f"📞 Stream started — SID: {stream_sid}")

                        async with pool.connection() as conn:
                            cur = await conn.execute(
                                """SELECT cim_name, ticket_id, priority, description
                                FROM incident_calls WHERE call_sid = %s""",
                                (call_sid,)
                            )#.fetchone()

                            row= await cur.fetchone()

                        # Debug 2 — did DB return anything?
                        print(f"🔍 DB row found: {row}")

                        incident = {
                            "cim_name":    row[0],
                            "id":          row[1],
                            "priority":    row[2],
                            "description": row[3],
                        } if row else {
                            "id": "UNKNOWN", "priority": "P1",
                            "description": "Unknown incident", "cim_name": "CIM",
                        }

                        await _initialize_session(openai_ws, incident)

                    elif event_type == "media":
                        if openai_ws.state.name == "OPEN":
                            await openai_ws.send(json.dumps({
                                "type":  "input_audio_buffer.append",
                                "audio": data["media"]["payload"],
                            }))

                    elif event_type == "stop":
                        print("📞 Twilio stream stopped")
                        break

            except WebSocketDisconnect:
                print("📞 Twilio disconnected")

        # ── Task 2: OpenAI → Twilio ──────────────────────────────────────
        async def send_to_twilio():
            nonlocal extracted_eta
            eta_obtained = False

            try:
                async for raw in openai_ws:
                    event = json.loads(raw)
                    etype = event.get("type", "")

                    if etype == "response.created":
                        if eta_obtained:
                            pass
                        else:
                            print("\n AI: ", end="", flush=True)

                    if etype == "response.output_audio.delta" and "delta" in event:
                        await twilio_ws.send_json({
                            "event":     "media",
                            "streamSid": stream_sid,
                            "media":     {"payload": event["delta"]},
                        })

                    elif etype == "response.output_audio_transcript.delta":
                        if not eta_obtained:
                            print(event.get("delta", ""), end="", flush=True)

                    elif etype == "response.output_audio.done":
                        print()
                        if eta_obtained:
                            print("📴 Audio complete — hanging up now.")
                            if call_sid:
                                #eta_store[call_sid] = extracted_eta
                                async with pool.connection() as conn:
                                    await conn.execute(
                                        "UPDATE incident_calls SET eta = %s WHERE call_sid = %s",
                                        (extracted_eta, call_sid),
                                    )
                                twilio_client.calls(call_sid).update(status="completed")
                            return
                        print("🎙️  Waiting for CIM...")

                    elif etype == "response.output_audio_transcript.done":
                        transcript = event.get("transcript", "")

                        if "ETA_CONFIRMED:" in transcript:
                            # Split the text directly at the marker
                            parts = transcript.split("ETA_CONFIRMED:")
                            after_marker = parts[1].strip()
                            
                            # Isolate the ETA by cutting off at the first period or newline
                            # This cleanly handles both single-line and multi-line responses
                            extracted_eta = after_marker.split(".")[0].strip()
                            
                            print(f"\n ETA captured: '{extracted_eta}'")
                            eta_obtained = True  # hang up deferred to audio.done

                        elif "got it" in transcript.lower() and any(char.isdigit() for char in transcript):
                            # Fallback to extract digits (e.g., "Got it, 15 minutes")
                            #import re
                            match = re.search(r'\d+\s*(?:mins?|minutes?)', transcript, re.IGNORECASE)
                            extracted_eta = match.group(0) if match else "Confirmed"
                            print(f"\n Fallback caught ETA: '{extracted_eta}'")
                            eta_obtained = True
                        
                    elif etype == "conversation.item.input_audio_transcription.completed":
                        cim_text = event.get("transcript", "").strip()
                        if cim_text:
                            print(f"\n  CIM : \"{cim_text}\"")
                            #pending_cim_transcript = cim_text

                    elif etype == "input_audio_buffer.speech_started":
                        last_item_id = None
                        if stream_sid:
                            await twilio_ws.send_json({
                                "event":     "clear",
                                "streamSid": stream_sid,
                            })

                    elif etype == "error":
                        print(f"\n❌ OpenAI error: {event.get('error', {}).get('message')}")
                        return

            except Exception as e:
                print(f"❌ send_to_twilio error: {e}")

        await asyncio.gather(receive_from_twilio(), send_to_twilio())

    if extracted_eta:
        print(f"🎯 Final result — CIM ETA: '{extracted_eta}'")
    else:
        print("⚠️  Call ended without ETA confirmation")


async def _initialize_session(openai_ws, incident: dict):
    """Sends session config and opening trigger to OpenAI Realtime."""
    await openai_ws.send(json.dumps({
        "type": "session.update",
        "session": {
            "type":              "realtime",
            "model":             OPENAI_REALTIME_MODEL,
            "output_modalities": ["audio"],
            "instructions":      build_system_prompt(incident),
            #"temperature" : 0.6,
            "audio": {
                "input": {
                    "format": {"type": "audio/pcmu"},
                    "turn_detection": {
                        "type":                "server_vad",
                        "threshold":           0.5,
                        "prefix_padding_ms":   200,
                        "silence_duration_ms": 500,
                    },
                    "transcription": {"model": "whisper-1"},
                },
                "output": {
                    "format": {"type": "audio/pcmu"},
                    "voice":  "alloy",
                },
            },
        },
    }))

    await openai_ws.send(json.dumps({
        "type": "conversation.item.create",
        "item": {
            "type":    "message",
            "role":    "user",
            "content": [{
                "type": "input_text",
                "text": (
                    f"The call just connected and {incident['cim_name']} picked up. "
                    f"Say a brief greeting only — introduce yourself. "
                    f"One sentence maximum. Then stop and wait."
                ),
            }],
        },
    }))

    await openai_ws.send(json.dumps({"type": "response.create"}))


