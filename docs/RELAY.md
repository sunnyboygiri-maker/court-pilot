# eCourts relay through an office/home PC (free, no ID needed)

eCourts' district portal blocks the server's network ("405 Security Page") but accepts ordinary
Indian broadband. This sends **only eCourts traffic** from the server through a PC on such a
connection, over Tailscale (a free private network between your own devices; nothing is exposed
to the internet).

**Limitation:** works only while that PC is on, awake and online. If it's off, case updates and
searches wait until it's back. Switch to a paid proxy later by changing one setting.

## 1. On the PC (once)
1. Install **Tailscale** from https://tailscale.com/download/windows and sign in (Google login works).
2. In Windows Settings → System → Power, set **Sleep: Never** while plugged in.
3. Double-click **`start-relay.bat`** in the CourtPilot folder. The window shows:
   `On the server set: ECOURTS_PROXY_URL=http://100.x.y.z:8899`. Note the `100.x.y.z` address.
   If Windows asks whether to allow Python on networks, choose **Allow** (private networks).
4. Optional, so it starts by itself after a restart: press Win+R, type `shell:startup`, and put a
   shortcut to `start-relay.bat` in that folder.

## 2. On the server (Hostinger browser terminal, once)
```bash
curl -fsSL https://tailscale.com/install.sh | sh && tailscale up
```
It prints a login link: open it and sign in with the **same** Tailscale account as the PC.

Then save the relay address (replace 100.x.y.z with the PC's address from step 1.3). This tests
eCourts through the relay from inside the app's container and only saves on success:
```bash
cd /opt/courtpilot && read -rp "PC relay address (100.x.y.z): " IP && P="http://$IP:8899" && C=$(curl -s -o /dev/null -w "%{http_code}" -x "$P" -A "Mozilla/5.0" --max-time 30 https://services.ecourts.gov.in/ecourtindia_v6/) && echo "eCourts through relay: $C" && if [ "$C" = 200 ]; then sed -i "/^ECOURTS_PROXY_URL=/d" .env && echo "ECOURTS_PROXY_URL=$P" >> .env && echo "Saved."; else echo "Not saved: is start-relay.bat running and Tailscale signed in on both?"; fi
```
Then `bash ~/update-server.sh`.

## Check it later
- On the PC, the relay window logs a line per eCourts connection.
- On the server: `docker compose -p courtpilot exec -T api python -c "import httpx; print(httpx.get('https://services.ecourts.gov.in/ecourtindia_v6/', proxy='http://100.x.y.z:8899', verify=False).status_code)"`
  should print 200.

## Switching to a paid proxy later
Run the IPRoyal command from docs/HANDOFF.md; it replaces `ECOURTS_PROXY_URL`. Then stop the relay.
