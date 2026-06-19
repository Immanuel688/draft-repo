"""
sheets_tool.py
--------------
Fetches team distribution lists and on-call CIM details
from Google Sheets.

Tab 1 — Teams:   team_id | display_name | dl_email | keywords_hint
Tab 2 — OnCall:  week_start | cim_name | cim_phone

IMPORTANT: Only keywords_hint is ever sent to the LLM.
           dl_email and cim_phone never leave this file.
"""

import os
import json
from datetime import date, datetime, timedelta,time #python's built in import for date and time
from google.oauth2.credentials import Credentials
from google.oauth2 import service_account
from googleapiclient.discovery import build

from dotenv import load_dotenv
load_dotenv(dotenv_path=".env") 


SPREADSHEET_ID1 = os.getenv("GOOGLE_SHEET_ID_TECH")# set in .env
SPREADSHEET_ID2 = os.getenv("GOOGLE_SHEET_ID_CIM")
SPREADSHEET_ID3 = os.getenv("GOOGLE_SHEET_ID_FIXED")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly"
]

def get_google_credentials() -> Credentials:
    """
    Loads Google credentials from a service account JSON file.
    Path is set via GOOGLE_SERVICE_ACCOUNT_FILE in .env
    """
    service_account_file = (
        "/secrets/sa/bridge-assistant-gsheet.json"
        if os.path.exists("/secrets/sa/bridge-assistant-gsheet.json")
        else os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
    )
    credentials_sheets = service_account.Credentials.from_service_account_file(
        service_account_file,
        scopes=SCOPES
    )
    return credentials_sheets

def fetch_teams(credentials_sheets: Credentials) -> list[dict]:
    """
    Returns all rows from the Teams tab.
    Each row: {id, display_name, dl_email, keywords_hint}
    """
    sheets = build("sheets", "v4", credentials=credentials_sheets)
    result = (
        sheets.spreadsheets()
        .values()
        .get(spreadsheetId=SPREADSHEET_ID1, range="A2:D")
        .execute()
    )
    rows = result.get("values", []) # If no row values return empty lists
    print (rows)
    teams = []
    for row in rows:
        if len(row) >= 3:
            teams.append({
                "display_name":  row[0].strip(),
                "dl_email":      row[1].strip(),
                "keywords_hint": row[2].strip(),
            })
    return teams

def parse_time_from_cell(cell: str) -> time:
    cell = cell.strip()
    if ":" in cell:
        h, m = cell.split(":")
        return time(int(h), int(m))
    elif "." in cell:
        h, m = cell.split(".")
        return time(int(h), int(m))
    else:
        return time(int(cell))

# ✅ New — built-in, no install needed
from zoneinfo import ZoneInfo

def get_current_time_in_tz(tz_name: str) -> time:
    tz_map = {
        "EST": "US/Eastern",
        "IST": "Asia/Kolkata",
        "PST": "US/Pacific",
        "CST": "US/Central",
        "UTC": "UTC",
    }
    if tz_name not in tz_map:
        raise ValueError(f"Unknown timezone abbreviation: {tz_name!r}")
    tz = ZoneInfo(tz_map[tz_name])
    return datetime.now(tz).time()


def fetch_current_cim(credentials_sheets) -> dict:
    sheets = build("sheets", "v4", credentials=credentials_sheets)
    result = (
        sheets.spreadsheets()
        .values()
        .get(spreadsheetId=SPREADSHEET_ID2, range="A10:I")
        .execute()
    )
    rows = result.get("values", [])
    today = date.today()
    primary = None
    backup_active = None

    for row in rows:
        # Need at least: name, email, contact, start_date, end_date
        if len(row) < 5:
            continue

        try:
            start_date = datetime.strptime(row[3].strip(), "%d/%m/%Y").date()
            end_date   = datetime.strptime(row[4].strip(), "%d/%m/%Y").date()

            # Skip expired rows
            if end_date < today:
                continue

            # Skip rows not yet active
            if not (start_date <= today <= end_date):
                continue

            name      = row[0].strip()
            email     = row[1].strip()
            phone     = row[2].strip()
            is_backup = len(row) > 5 and row[5].strip().lower() == "yes"

            if is_backup and len(row) >= 9:
                start_t      = parse_time_from_cell(row[6])
                end_t        = parse_time_from_cell(row[7])
                timezone     = row[8].strip()
                current_time = get_current_time_in_tz(timezone)

                if start_t <= current_time <= end_t:
                    backup_active = {"name": name,
                                     "role": "backup_active_now",
                                     "email":  email ,
                                     "phone": phone}

            else:
                primary = {"name": name,
                           "role": "primary",
                           "email":  email ,
                           "phone": phone}


        except (ValueError, IndexError, AttributeError):
            continue

    if backup_active:
        return backup_active
    if primary:
        return primary

    raise ValueError(f"No CIM found for today ({today})")

def fetch_fixed_members(credentials_sheets: Credentials) -> dict:
    """
    Returns fixed attendees from the FixedMembers tab.
    Tab 3 — FixedMembers: role | email | attendance_type (required/optional)

    Returns:
        {
            "stakeholder_dl": "stakeholder@company.com",
            "optional":       ["it.mgr@company.com", ...]
        }
    """
    sheets = build("sheets", "v4", credentials=credentials_sheets)
    result = (
        sheets.spreadsheets()
        .values()
        .get(spreadsheetId=SPREADSHEET_ID3, range="A2:C")
        .execute()
    )
    rows = result.get("values", [])
    print(rows)

    stakeholder_dl = None
    optional = []

    for row in rows:
        if len(row) >= 3:
            role            = row[0].strip()
            email           = row[1].strip()
            attendance_type = row[2].strip().lower()

            if role.lower() == "stakeholder":
                stakeholder_dl = email
            elif attendance_type == "optional":
                optional.append(email)

    if not stakeholder_dl:
        raise ValueError(
            "Stakeholder DL not found in FixedMembers tab. "
            "Please add a row with role='Stakeholder'."
        )

    return {
        "stakeholder_dl": stakeholder_dl,
        "optional":       optional,
    }

