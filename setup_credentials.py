"""Derive CLOB API credentials from your private key.

Run once:  python setup_credentials.py
Then copy the printed values into your .env file.
"""

import os
import sys

from dotenv import load_dotenv

load_dotenv()

pk = os.environ.get("POLYMARKET_PRIVATE_KEY")
if not pk:
    print("ERROR: POLYMARKET_PRIVATE_KEY not set in .env")
    sys.exit(1)

from py_clob_client.client import ClobClient

# Derive credentials (signature_type=2 for Polymarket proxy wallet)
client = ClobClient(
    "https://clob.polymarket.com",
    key=pk,
    chain_id=137,
    signature_type=2,
)

print("Deriving CLOB API credentials...")
creds = client.create_or_derive_api_creds()

print()
print("Add these to your .env file:")
print("─" * 50)
print(f"CLOB_API_KEY={creds.api_key}")
print(f"CLOB_API_SECRET={creds.api_secret}")
print(f"CLOB_API_PASSPHRASE={creds.api_passphrase}")
print("─" * 50)
