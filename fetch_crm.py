
import json
import os
import requests
import tkinter as tk
from tkinter import simpledialog

BASE_URL = "https://analyst-assessment-production.up.railway.app/api/v1"

# Enter token securely
root = tk.Tk()
root.withdraw()
root.attributes("-topmost", True)

token = simpledialog.askstring(
    "Clipboard API",
    "Enter your Personal API Token:",
    show="*",
    parent=root
)

root.destroy()

if not token:
    raise SystemExit("No token entered.")

token = token.strip()

if token.lower().startswith("bearer "):
    token = token[7:].strip()

headers = {
    "Authorization": f"Bearer {token}",
    "Accept": "application/json"
}

all_accounts = []
page = 1
page_size = 50

while page <= 20:
    response = requests.get(
        f"{BASE_URL}/accounts",
        headers=headers,
        params={
            "page": page,
            "page_size": page_size
        },
        timeout=30
    )

    response.raise_for_status()
    result = response.json()

    accounts = result.get("data", [])

    if not isinstance(accounts, list):
        raise ValueError("Unexpected API response format.")

    if not accounts:
        break

    all_accounts.extend(accounts)

    print(f"Page {page}: {len(accounts)} accounts")

    total = result.get("total")

    if total is not None and len(all_accounts) >= int(total):
        break

    if len(accounts) < page_size:
        break

    page += 1

# Save the complete CRM snapshot
os.makedirs("data", exist_ok=True)

with open("data/crm_accounts.json", "w", encoding="utf-8") as file:
    json.dump(all_accounts, file, indent=2, ensure_ascii=False)

print("\nCRM DATA DOWNLOAD COMPLETE")
print("Total accounts:", len(all_accounts))
print("Saved to: data/crm_accounts.json")

if all_accounts:
    print("\nAvailable CRM fields:")
    for field in all_accounts[0].keys():
        print("-", field)
