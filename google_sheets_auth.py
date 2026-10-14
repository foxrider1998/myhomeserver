#!/usr/bin/env python3
"""
google_sheets_auth.py - Google Sheets OAuth Authenticator & Sync Engine for Hermes QA
"""

import os
import sys
import json

# Izinkan http://localhost untuk Desktop OAuth redirect
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive.file'
]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CLIENT_SECRET_FILE = os.path.join(BASE_DIR, 'client_secret.json')
TOKEN_FILE = os.path.join(BASE_DIR, 'token.json')


def get_credentials():
    """Returns valid Google OAuth credentials, refreshing or creating if needed."""
    creds = None
    if os.path.exists(TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        except Exception as e:
            print(f"[WARN] Error loading existing token: {e}")
            creds = None

    if creds and creds.expired and creds.refresh_token:
        try:
            print("[INFO] Refreshing expired token...")
            creds.refresh(Request())
            with open(TOKEN_FILE, 'w') as f:
                f.write(creds.to_json())
            print("[INFO] Token refreshed successfully.")
            return creds
        except Exception as e:
            print(f"[WARN] Failed to refresh token: {e}")
            creds = None

    if not creds or not creds.valid:
        if not os.path.exists(CLIENT_SECRET_FILE):
            raise FileNotFoundError(f"Missing {CLIENT_SECRET_FILE}")

        flow = InstalledAppFlow.from_client_secrets_file(
            CLIENT_SECRET_FILE,
            scopes=SCOPES,
            redirect_uri='http://localhost'
        )
        auth_url, _ = flow.authorization_url(prompt='consent', access_type='offline')

        print("\n" + "=" * 70)
        print("GOOGLE OAUTH 2.0 LOGIN FOR HERMES QA")
        print("=" * 70)
        print("\n1. Buka tautan berikut di browser kamu:\n")
        print(auth_url)
        print("\n" + "-" * 70)
        print("2. Login dengan akun Google kamu dan klik 'Lanjutkan / Allow'.")
        print("3. Setelah berhasil, browser akan diarahkan ke halaman http://localhost/?code=...")
        print("   (Meskipun halaman bertuliskan 'Unable to connect' atau 'This site can't be reached', itu normal!)")
        print("4. Copy SELURUH link dari address bar browser kamu, lalu paste di bawah ini:")
        print("=" * 70 + "\n")

        redirect_response = input("Paste link dari address bar di sini: ").strip()
        if not redirect_response:
            print("[ERROR] Input kosong.")
            sys.exit(1)

        try:
            flow.fetch_token(authorization_response=redirect_response)
        except Exception:
            try:
                from urllib.parse import urlparse, parse_qs
                parsed = parse_qs(urlparse(redirect_response).query)
                extracted_code = parsed.get('code', [None])[0] if parsed.get('code') else redirect_response
                flow.fetch_token(code=extracted_code)
            except Exception as e2:
                print(f"[ERROR] Gagal memproses token: {e2}")
                sys.exit(1)

        creds = flow.credentials
        with open(TOKEN_FILE, 'w') as f:
            f.write(creds.to_json())
        print(f"\n[SUCCESS] Token berhasil disimpan di: {TOKEN_FILE}")

    return creds


def get_sheets_service():
    """Builds and returns the Google Sheets service client."""
    creds = get_credentials()
    return build('sheets', 'v4', credentials=creds)


def test_connection():
    """Validates connection by initializing the Sheets API client."""
    service = get_sheets_service()
    print("[SUCCESS] Terhubung ke Google Sheets API v4!")
    return service


if __name__ == '__main__':
    try:
        test_connection()
    except Exception as e:
        print(f"[ERROR] {e}")
