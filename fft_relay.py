"""Bridges PlutoDVB2's local RX WebFFT service to the LAN.

Why this exists: ws://192.168.2.1:7681/websocket only exists on the private
USB link between this Jetson and the Pluto - a browser on another machine
(e.g. a Windows PC) has no route to 192.168.2.1 and can never connect to it
directly, no matter where fft_viewer.html itself is served from. This
process runs on the Jetson (which can reach both networks) and re-serves:
  - fft_viewer.html itself over plain HTTP (port 8000), and
  - the FFT frames over a second WebSocket (port 8765) reachable from any
    machine on the LAN,
so fft_viewer.html can be opened from any browser on the network instead of
needing the Jetson's own desktop/VNC.

Run this alongside datv_tx_plus_fft.py (which enables the Pluto's WebFFT
service over MQTT) - this script only relays frames it already publishes,
it does not configure the Pluto itself.
"""

import asyncio
import http.server
import socket
import socketserver
import threading

import websockets

PLUTO_WS_URL = "ws://192.168.2.1:7681/websocket"
HTTP_PORT = 8000
RELAY_WS_PORT = 8765

clients = set()


def start_http_server():
    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("0.0.0.0", HTTP_PORT), handler) as httpd:
        httpd.serve_forever()


async def broadcast(message):
    dead = set()
    for client in clients:
        try:
            await client.send(message)
        except websockets.ConnectionClosed:
            dead.add(client)
    clients.difference_update(dead)


async def pluto_reader():
    while True:
        try:
            async with websockets.connect(PLUTO_WS_URL) as pluto_ws:
                print("Connected to Pluto FFT at {}".format(PLUTO_WS_URL))
                async for message in pluto_ws:
                    await broadcast(message)
        except Exception as exc:
            print("Pluto FFT connection lost/failed ({}); retrying in 2s...".format(exc))
            await asyncio.sleep(2)


async def browser_handler(websocket, _path):
    clients.add(websocket)
    print("Browser client connected ({} total)".format(len(clients)))
    try:
        async for _ in websocket:
            pass  # browsers don't send anything back; just keep the socket open
    except websockets.ConnectionClosed:
        pass
    finally:
        clients.discard(websocket)
        print("Browser client disconnected ({} total)".format(len(clients)))


def get_lan_ip():
    """Best-effort LAN IP for this Jetson - NOT the Pluto's 192.168.2.x USB
    address. Opens a UDP "connection" (no packet actually sent for UDP) to
    pick whichever local interface the OS would route external traffic
    through, then reads that socket's own address back.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    threading.Thread(target=start_http_server, daemon=True).start()
    lan_ip = get_lan_ip()
    print("Static page:     http://{}:{}/fft_viewer.html".format(lan_ip, HTTP_PORT))
    print("Relay WebSocket: ws://{}:{}/".format(lan_ip, RELAY_WS_PORT))

    loop = asyncio.get_event_loop()
    loop.run_until_complete(websockets.serve(browser_handler, "0.0.0.0", RELAY_WS_PORT))
    loop.run_until_complete(pluto_reader())


if __name__ == "__main__":
    main()
