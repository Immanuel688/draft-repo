"""
calendar_tool.py
----------------
Creates a Google Calendar event with a Meet link.
Accepts required and optional attendees separately.
"""

import os
import uuid
import pickle
from datetime import datetime, timezone, timedelta
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
#from google_auth_oauthlib.flow import InstalledAppFlow

CALENDAR_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
]

def get_calendar_credentials() -> Credentials:
    credentials_calendar = None
    TOKEN_PATH = os.getenv("CALENDAR_TOKEN_PATH", "/secrets/cal/token.pickle")
    if not os.path.exists(TOKEN_PATH):
        TOKEN_PATH = "token.pickle"  # local fallback

    if os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH, "rb") as token:
            credentials_calendar = pickle.load(token)

    if not credentials_calendar or not credentials_calendar.valid:
        if credentials_calendar and credentials_calendar.expired and credentials_calendar.refresh_token:
            credentials_calendar.refresh(Request())
        else:
            raise RuntimeError(
                "No valid calendar token found. "
                "Upload token.pickle to Secret Manager."
            )


    return credentials_calendar


def send_calendar_invite(invite: dict, credentials_calendar: Credentials) -> str:
    """
    Creates a Google Calendar event and returns the Meet link.

    invite dict keys:
        title              : str
        description        : str
        start_time         : ISO8601 string
        end_time           : ISO8601 string
        required_attendees : list[str]   - emails
        optional_attendees : list[str]   - emails
        ticket_id          : str         - used as conference request ID
    """
    service = build("calendar", "v3", credentials=credentials_calendar)

    # Build attendee list — required first, then optional
    attendees = []
    for email in invite["required_attendees"]:
        attendees.append({"email": email, "optional": False})
    for email in invite["optional_attendees"]:
        attendees.append({"email": email, "optional": True})

    event_body = {
        "summary":     invite["title"],
        "description": invite["description"],
        "start": {
            "dateTime": invite["start_time"],
            "timeZone": "Asia/Kolkata",      # adjust to your timezone
        },
        "end": {
            "dateTime": invite["end_time"],
            "timeZone": "Asia/Kolkata",
        },
        "attendees": attendees,
        "conferenceData": {
            "createRequest": {
                "requestId":             invite["ticket_id"],
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
        "reminders": {
            "useDefault": False,
            "overrides":  [{"method": "popup", "minutes": 5}],
        },
    }

    created_event = (
        service.events()
        .insert(
            calendarId="primary",
            body=event_body,
            conferenceDataVersion=1,
            sendUpdates="all",   # sends email invites to all attendees
        )
        .execute()
    )

    meet_link = created_event.get("hangoutLink", "")
    return meet_link
