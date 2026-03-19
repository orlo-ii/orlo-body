#!/usr/bin/env python3
"""
Echo body — minimal test adapter for development.

Connects to the hub, sends periodic fake state updates,
and logs any actions received from Orlo.
"""

import asyncio
import json
import math
import time

import websockets


HUB_URL = "ws://127.0.0.1:9500"
BODY_ID = "echo-1"


async def main():
    async with websockets.connect(HUB_URL) as ws:
        # Handshake
        await ws.send(json.dumps({
            "type": "hello",
            "body_id": BODY_ID,
            "body_type": "echo",
            "body_name": "Echo Test Body",
            "capabilities": ["move", "look", "say"],
            "version": "0.1.0",
        }))

        welcome = json.loads(await ws.recv())
        print(f"Connected: {welcome}")

        # Start state loop and action listener concurrently
        await asyncio.gather(
            _send_states(ws),
            _receive_actions(ws),
        )


async def _send_states(ws):
    """Send fake state every 5 seconds."""
    tick = 0
    while True:
        tick += 1
        x = math.sin(tick * 0.1) * 10
        z = math.cos(tick * 0.1) * 10

        state = {
            "type": "state",
            "ts": time.time(),
            "body_id": BODY_ID,
            "pose": {"x": x, "y": 64.0, "z": z, "yaw": tick * 10 % 360, "pitch": 0.0},
            "velocity": {"x": 0.0, "y": 0.0, "z": 0.0},
            "vitals": {"health": 1.0, "energy": 0.9, "status": "idle"},
            "environment": {
                "nearby_entities": [
                    {"name": "TestPlayer", "type": "player", "distance": 5.0, "bearing": 90.0}
                ],
                "nearby_objects": [
                    {"name": "rock", "type": "block", "distance": 2.0}
                ],
                "surface": "test_biome",
                "time": {"period": "morning", "raw": tick * 100},
            },
            "inventory": {
                "items": [{"name": "stick", "count": 3}],
                "equipped": "stick",
            },
        }

        await ws.send(json.dumps(state))
        print(f"[state #{tick}] pos: ({x:.1f}, 64, {z:.1f})")
        await asyncio.sleep(5)


async def _receive_actions(ws):
    """Listen for actions from Orlo and log them."""
    async for raw in ws:
        msg = json.loads(raw)
        if msg.get("type") == "action":
            print(f"[action] {msg['action']}({msg.get('params', {})})")
        elif msg.get("type") == "query":
            print(f"[query] {msg['query']}")
            # Respond to capabilities query
            if msg["query"] == "capabilities":
                await ws.send(json.dumps({
                    "type": "query_response",
                    "query_id": msg.get("query_id"),
                    "data": {
                        "capabilities": ["move", "look", "say"],
                        "body_type": "echo",
                    },
                }))
        else:
            print(f"[unknown] {msg}")


if __name__ == "__main__":
    asyncio.run(main())
