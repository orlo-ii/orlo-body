#!/usr/bin/env python3
"""
orlo-body hub — WebSocket router between Orlo and body adapters.

Thin relay: bodies connect and send perception, Orlo connects and sends actions.
The hub routes messages by body_id and maintains a registry of connected bodies.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import websockets
from websockets.server import WebSocketServerProtocol

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("hub")

HOST = "127.0.0.1"
PORT = 9500


@dataclass
class ConnectedBody:
    body_id: str
    body_type: str
    body_name: str
    capabilities: list[str]
    ws: WebSocketServerProtocol
    connected_at: float = field(default_factory=time.time)


class Hub:
    def __init__(self):
        self.bodies: dict[str, ConnectedBody] = {}
        self.orlo_ws: Optional[WebSocketServerProtocol] = None

    async def handle_connection(self, ws: WebSocketServerProtocol):
        """Handle a new WebSocket connection (body or Orlo)."""
        try:
            # First message determines if this is a body or Orlo
            raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
            msg = json.loads(raw)

            if msg.get("type") == "hello":
                await self._handle_body(ws, msg)
            elif msg.get("type") == "orlo_connect":
                await self._handle_orlo(ws, msg)
            else:
                log.warning("Unknown initial message type: %s", msg.get("type"))
                await ws.close(1002, "Send hello or orlo_connect first")
        except Exception as e:
            log.error("Connection error: %s", e)

    async def _handle_body(self, ws: WebSocketServerProtocol, hello: dict):
        """Handle a body adapter connection."""
        body_id = hello["body_id"]
        body = ConnectedBody(
            body_id=body_id,
            body_type=hello.get("body_type", "unknown"),
            body_name=hello.get("body_name", body_id),
            capabilities=hello.get("capabilities", []),
            ws=ws,
        )
        self.bodies[body_id] = body
        log.info("Body connected: %s (%s)", body.body_name, body_id)

        # Send welcome
        await ws.send(json.dumps({
            "type": "welcome",
            "session_id": f"{body_id}-{int(time.time())}",
        }))

        # Notify Orlo that a body came online
        if self.orlo_ws:
            await self._send_orlo({
                "type": "event",
                "ts": time.time(),
                "body_id": body_id,
                "event": "body_connected",
                "data": {
                    "body_id": body_id,
                    "body_type": body.body_type,
                    "body_name": body.body_name,
                    "capabilities": body.capabilities,
                },
            })

        try:
            async for raw in ws:
                msg = json.loads(raw)
                # Forward perception messages to Orlo
                if msg.get("type") in ("state", "event"):
                    msg.setdefault("body_id", body_id)
                    msg.setdefault("ts", time.time())
                    await self._send_orlo(msg)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            del self.bodies[body_id]
            log.info("Body disconnected: %s", body_id)
            if self.orlo_ws:
                await self._send_orlo({
                    "type": "event",
                    "ts": time.time(),
                    "body_id": body_id,
                    "event": "body_disconnected",
                    "data": {"body_id": body_id},
                })

    async def _handle_orlo(self, ws: WebSocketServerProtocol, msg: dict):
        """Handle Orlo (Hermes) connecting to the hub."""
        if self.orlo_ws:
            log.warning("Orlo reconnected, dropping old connection")
            try:
                await self.orlo_ws.close(1000, "Replaced by new connection")
            except Exception:
                pass

        self.orlo_ws = ws
        log.info("Orlo connected")

        # Send current body registry
        await ws.send(json.dumps({
            "type": "bodies",
            "bodies": [
                {
                    "body_id": b.body_id,
                    "body_type": b.body_type,
                    "body_name": b.body_name,
                    "capabilities": b.capabilities,
                }
                for b in self.bodies.values()
            ],
        }))

        try:
            async for raw in ws:
                msg = json.loads(raw)
                # Route actions to the correct body
                if msg.get("type") in ("action", "query"):
                    body_id = msg.get("body_id")
                    if body_id and body_id in self.bodies:
                        await self.bodies[body_id].ws.send(json.dumps(msg))
                    elif body_id:
                        await ws.send(json.dumps({
                            "type": "error",
                            "error": f"Body '{body_id}' not connected",
                        }))
                    else:
                        # Broadcast to all bodies
                        for body in self.bodies.values():
                            await body.ws.send(json.dumps(msg))
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self.orlo_ws = None
            log.info("Orlo disconnected")

    async def _send_orlo(self, msg: dict):
        """Send a message to Orlo if connected."""
        if self.orlo_ws:
            try:
                await self.orlo_ws.send(json.dumps(msg))
            except Exception as e:
                log.error("Failed to send to Orlo: %s", e)


async def main():
    hub = Hub()
    log.info("orlo-body hub starting on ws://%s:%d", HOST, PORT)
    async with websockets.serve(hub.handle_connection, HOST, PORT):
        log.info("Hub ready. Waiting for connections...")
        await asyncio.Future()  # Run forever


if __name__ == "__main__":
    main_loop = asyncio.run(main())
