#!/usr/bin/env python3
"""One-time Google OAuth2 setup for Gmail + Calendar access.

Run this once to authorize and save a refresh token:
    python3 google_auth_setup.py

Prerequisites:
1. Go to https://console.cloud.google.com/apis/credentials
2. Create an OAuth 2.0 Client ID (type: Desktop app)
3. Download the JSON and save it as 'google_credentials.json' in this directory
4. Enable Gmail API and Google Calendar API in the project:
   https://console.cloud.google.com/apis/library/gmail.googleapis.com
   https://console.cloud.google.com/apis/library/calendar-json.googleapis.com
"""

import os
import json
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/calendar.readonly',
]

DIR = os.path.dirname(os.path.abspath(__file__))
CREDS_FILE = os.path.join(DIR, 'google_credentials.json')
TOKEN_FILE = os.path.join(DIR, 'google_token.json')


def main():
    if not os.path.exists(CREDS_FILE):
        print(f"\nERROR: {CREDS_FILE} not found.")
        print("\nTo set up:")
        print("1. Go to https://console.cloud.google.com/apis/credentials")
        print("2. Click '+ CREATE CREDENTIALS' > 'OAuth client ID'")
        print("3. Application type: 'Desktop app'")
        print("4. Download the JSON file")
        print(f"5. Save it as: {CREDS_FILE}")
        print("\nAlso enable these APIs:")
        print("  https://console.cloud.google.com/apis/library/gmail.googleapis.com")
        print("  https://console.cloud.google.com/apis/library/calendar-json.googleapis.com")
        return

    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("Refreshing existing token...")
            creds.refresh(Request())
        else:
            print("Opening browser for Google authorization...")
            flow = InstalledAppFlow.from_client_secrets_file(CREDS_FILE, SCOPES)
            creds = flow.run_local_server(port=8089)

        with open(TOKEN_FILE, 'w') as f:
            f.write(creds.to_json())
        print(f"\nToken saved to {TOKEN_FILE}")
    else:
        print("Token is still valid.")

    # Quick test
    from googleapiclient.discovery import build
    try:
        gmail = build('gmail', 'v1', credentials=creds)
        profile = gmail.users().getProfile(userId='me').execute()
        print(f"Gmail connected: {profile['emailAddress']}")
    except Exception as e:
        print(f"Gmail test failed: {e}")

    try:
        cal = build('calendar', 'v3', credentials=creds)
        cals = cal.calendarList().list(maxResults=3).execute()
        print(f"Calendar connected: {len(cals.get('items', []))} calendars found")
    except Exception as e:
        print(f"Calendar test failed: {e}")

    print("\nSetup complete! The mobile app will now show your Work tab.")


if __name__ == '__main__':
    main()
