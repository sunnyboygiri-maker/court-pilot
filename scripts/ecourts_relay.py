"""
eCourts relay: lets the CourtPilot server reach eCourts through this computer's
internet connection.

eCourts' district portal refuses many data-centre networks (our server gets
"405 Security Page") but accepts ordinary Indian broadband. Run this on a PC on
such a connection; the server sets ECOURTS_PROXY_URL=http://<this PC>:8899.

Safety:
- Only HTTPS tunnels (CONNECT) to *.ecourts.gov.in are allowed; anything else is
  refused, so this can't be used as a general proxy.
- Listens on one address you choose (default: the Tailscale address, a private
  network only your own devices are on). Never expose it to the internet.

Run:  python scripts/ecourts_relay.py [--listen 100.x.y.z] [--port 8899]
      (or double-click start-relay.bat)
"""
import argparse
import asyncio
import logging
import re
import subprocess

ALLOWED_HOST = re.compile(r"^([a-z0-9-]+\.)*ecourts\.gov\.in$", re.I)
ALLOWED_PORTS = {443}
IDLE_TIMEOUT = 120
MAX_CLIENTS = 32

log = logging.getLogger("ecourts-relay")
_slots = asyncio.Semaphore(MAX_CLIENTS)


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await asyncio.wait_for(reader.read(65536), IDLE_TIMEOUT)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (asyncio.TimeoutError, ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _refuse(writer: asyncio.StreamWriter, status: str) -> None:
    writer.write(f"HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
    try:
        await writer.drain()
    finally:
        writer.close()


async def handle(client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
    peer = client_w.get_extra_info("peername")
    async with _slots:
        try:
            head = await asyncio.wait_for(client_r.readuntil(b"\r\n\r\n"), 15)
        except Exception:
            client_w.close()
            return
        request_line = head.split(b"\r\n", 1)[0].decode("latin-1")
        parts = request_line.split()
        if len(parts) != 3 or parts[0].upper() != "CONNECT":
            log.warning("Refused %s from %s (only HTTPS tunnels to eCourts)", request_line[:60], peer)
            await _refuse(client_w, "405 Method Not Allowed")
            return
        host, _, port = parts[1].rpartition(":")
        if not ALLOWED_HOST.match(host) or not port.isdigit() or int(port) not in ALLOWED_PORTS:
            log.warning("Refused tunnel to %s from %s (not eCourts)", parts[1], peer)
            await _refuse(client_w, "403 Forbidden")
            return
        try:
            up_r, up_w = await asyncio.wait_for(asyncio.open_connection(host, int(port)), 20)
        except Exception as e:
            log.warning("Couldn't reach %s: %s", parts[1], e)
            await _refuse(client_w, "502 Bad Gateway")
            return
        client_w.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await client_w.drain()
        log.info("Tunnel %s -> %s", peer[0] if peer else "?", host)
        await asyncio.gather(_pipe(client_r, up_w), _pipe(up_r, client_w))


def tailscale_ip() -> str:
    # Right after installing, Tailscale may not be on PATH yet: try the default install location too
    for exe in ("tailscale", r"C:\Program Files\Tailscale\tailscale.exe"):
        try:
            out = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=10)
        except Exception:
            continue
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip().splitlines()[0]
    return ""


async def main() -> None:
    parser = argparse.ArgumentParser(description="Relay eCourts traffic for the CourtPilot server")
    parser.add_argument("--listen", default="", help="address to listen on (default: this PC's Tailscale address)")
    parser.add_argument("--port", type=int, default=8899)
    args = parser.parse_args()
    listen = args.listen or tailscale_ip()
    if not listen:
        raise SystemExit("Tailscale isn't running on this PC. Install it and sign in (see docs/RELAY.md), "
                         "or pass --listen 127.0.0.1 to test locally.")
    server = await asyncio.start_server(handle, listen, args.port)
    print(f"\neCourts relay running on {listen}:{args.port}")
    print(f"On the server set: ECOURTS_PROXY_URL=http://{listen}:{args.port}")
    print("Only eCourts traffic is allowed through. Keep this window open (or minimised).\n", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    asyncio.run(main())
