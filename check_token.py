import os
import requests

cid = os.environ.get("YT_CLIENT_ID", "").strip()
cs = os.environ.get("YT_CLIENT_SECRET", "").strip()
rt = os.environ.get("YT_REFRESH_TOKEN", "").strip()

print("Client ID: project number =", cid.split("-")[0] if "-" in cid else "?",
      "| ends with .apps.googleusercontent.com:", cid.endswith(".apps.googleusercontent.com"))
print("Client secret: length", len(cs), "| starts with GOCSPX-:", cs.startswith("GOCSPX-"))
print("Refresh token: length", len(rt), "| starts with 1//:", rt.startswith("1//"))

if not (cid and cs and rt):
    raise SystemExit("A secret is EMPTY. Check the secret names in GitHub.")

try:
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": cid, "client_secret": cs,
        "refresh_token": rt, "grant_type": "refresh_token"}, timeout=30)
except Exception as e:
    raise SystemExit(f"Network error: {e}")

data = r.json()
if r.ok and "access_token" in data:
    print("RESULT: TOKEN OK")
    print("Scope:", data.get("scope"))
else:
    print("RESULT: FAILED", r.status_code, data.get("error"), "-", data.get("error_description"))
    if data.get("error") == "invalid_client":
        print("MEANING: Client ID or Client secret is wrong.")
    elif data.get("error") == "invalid_grant":
        print("MEANING: The refresh token does not belong to this Client ID/secret, or it expired or was revoked.")
    raise SystemExit(1)
