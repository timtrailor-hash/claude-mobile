#!/usr/bin/env python3
"""Google OAuth2 setup for Gmail + Calendar access (supports multiple accounts).

Usage:
    python3 google_auth_setup.py              # Set up personal account
    python3 google_auth_setup.py work         # Set up work account
"""

import os
import sys
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/calendar.readonly',
    'https://www.googleapis.com/auth/drive',
]

DIR = os.path.dirname(os.path.abspath(__file__))
CREDS_FILE = os.path.join(DIR, 'google_credentials.json')

ACCOUNTS = {
    'personal': {
        'token_file': os.path.join(DIR, 'google_token.json'),
        'label': 'Personal',
    },
    'work': {
        'token_file': os.path.join(DIR, 'google_token_work.json'),
        'label': 'Work',
    },
}


def setup_account(account_key):
    account = ACCOUNTS[account_key]
    token_file = account['token_file']
    label = account['label']

    print(f"\n=== Setting up {label} account ===")

    # Load OAuth client config: JSON file first, then credentials.py
    client_config = None
    if not os.path.exists(CREDS_FILE):
        try:
            sys.path.insert(0, os.path.dirname(DIR))
            from credentials import GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET, GOOGLE_OAUTH_PROJECT_ID
            client_config = {
                "installed": {
                    "client_id": GOOGLE_OAUTH_CLIENT_ID,
                    "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
                    "project_id": GOOGLE_OAUTH_PROJECT_ID,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"],
                }
            }
            print("Using OAuth credentials from credentials.py")
        except ImportError:
            print(f"\nERROR: No OAuth credentials found.")
            print(f"Add GOOGLE_OAUTH_CLIENT_ID/SECRET to credentials.py")
            print(f"or save google_credentials.json in: {DIR}")
            return

    creds = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("Refreshing existing token...")
            creds.refresh(Request())
        else:
            print(f"Opening browser — please sign in with your {label} Google account...")
            if client_config:
                flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
            else:
                flow = InstalledAppFlow.from_client_secrets_file(CREDS_FILE, SCOPES)
            creds = flow.run_local_server(port=8089)

        with open(token_file, 'w') as f:
            f.write(creds.to_json())
        print(f"Token saved to {token_file}")
    else:
        print("Token is still valid.")

    # Quick test
    from googleapiclient.discovery import build
    try:
        gmail = build('gmail', 'v1', credentials=creds, cache_discovery=False)
        profile = gmail.users().getProfile(userId='me').execute()
        print(f"Gmail connected: {profile['emailAddress']}")
    except Exception as e:
        print(f"Gmail test failed: {e}")

    try:
        cal = build('calendar', 'v3', credentials=creds, cache_discovery=False)
        cals = cal.calendarList().list(maxResults=3).execute()
        print(f"Calendar connected: {len(cals.get('items', []))} calendars found")
    except Exception as e:
        print(f"Calendar test failed: {e}")

    print(f"\n{label} setup complete!")


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else 'personal'
    if arg not in ACCOUNTS:
        print(f"Unknown account: {arg}")
        print(f"Usage: python3 google_auth_setup.py [{'/'.join(ACCOUNTS.keys())}]")
        return
    setup_account(arg)


if __name__ == '__main__':
    main()
