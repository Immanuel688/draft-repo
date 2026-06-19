import logging
import os
import httpx

log = logging.getLogger(__name__)

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")

async def trigger_cim_call(thread_id: str, cim: dict, invite: dict):
    """
    Fires POST /internal/call via httpx.
    Runs as a FastAPI BackgroundTask — errors are logged, not raised.
    """
    payload = {
        "thread_id":   thread_id,
        "cim_name":    cim.get("name", ""),
        "cim_phone":   cim.get("phone", ""),
        "ticket_id":   invite.get("ticket_id", ""),
        "priority":    invite.get("priority", ""),
        "description": invite.get("subject", ""),
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{PUBLIC_BASE_URL}/internal/call",
                json=payload,
                timeout=60.0,
            )
            resp.raise_for_status()
            log.info("CIM call triggered: %s", resp.json())
    except Exception as exc:
        log.error("trigger_cim_call failed: %s", exc)