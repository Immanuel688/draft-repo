from typing import TypedDict, Optional

class IncidentState(TypedDict):
    # --- INPUT fields (provided by service desk agent) ---
    ticket_id:         str
    priority:          str           # P1 / P2
    subject:           str           # e.g. "WiFi down plant-wide"
    context:           str           # additional incident context

    # --- RAG node output ---
    recipient_emails:  list[str]     # tech DL emails matched from Sheets
    cim:               dict          # {"name": "Ravi", "phone": "+91XXX"}
    fixed:             dict          # fixed emails in the invite

    # --- Draft node output ---
    draft_invite:      Optional[dict]

    # --- Human approval output ---
    approved_invite:   Optional[dict]

    # --- Calendar node output ---
    meet_link:         Optional[str]

    # cim eta capture
    #cim_eta:           Optional[str]