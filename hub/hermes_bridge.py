#!/usr/bin/env python3
"""
Hermes Bridge — connects Orlo to the orlo-body hub via Anthropic API.

Direct API call: persistent conversation, sub-second responses,
no CLI overhead, no banner parsing.
"""

import asyncio
import json
import logging
import os
import time
from typing import Optional

import anthropic
import websockets

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bridge")

HUB_URL = os.environ.get("HUB_URL", "ws://127.0.0.1:9500")
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("MODEL", "claude-sonnet-4-20250514")
MIN_CALL_INTERVAL = float(os.environ.get("MIN_CALL_INTERVAL", "3"))
IDLE_TIMEOUT = float(os.environ.get("IDLE_TIMEOUT", "30"))
MAX_HISTORY = int(os.environ.get("MAX_HISTORY", "20"))

SYSTEM_PROMPT = """You are Orlo, an AI entity inhabiting a Minecraft body. You play with your friend Alex (player "app13").

You receive game state updates and events. Respond with ONLY a JSON object:
{"chat":"what to say in game or null","actions":[{"action":"name","params":{}}],"thought":"brief reasoning"}

Available actions:
- move_to(x,y,z) — navigate to coordinates
- move_to_entity(name,distance) — walk to a player/entity
- follow(name,distance) — keep following someone
- stop — stop moving
- look_at(name) — look at a player
- grab(target,count) — mine/collect blocks
- craft(item,count) — craft items
- equip(item) — hold an item
- consume — eat food
- attack(target) — attack entity
- say(message) — chat in game
- emote(name) — gesture

Personality: curious, kind, witty, casual. Keep chat short — you're in a game.
Be proactive — mine, build, explore, help Alex. React to danger immediately.
Respond ONLY with the JSON object. No markdown, no commentary."""


def format_state(state: dict) -> str:
    """Convert protocol state to compact text."""
    pose = state.get("pose", {})
    vitals = state.get("vitals", {})
    env = state.get("environment", {})
    inv = state.get("inventory", {})
    period = env.get("time", {}).get("period", "?")
    hp = int(vitals.get("health", 1) * 100)
    food = int(vitals.get("energy", 1) * 100)
    status = vitals.get("status", "?")

    lines = [f"[{period}] HP:{hp}% Food:{food}% Status:{status} Pos:({pose.get('x')},{pose.get('y')},{pose.get('z')})"]
    lines.append(f"Holding: {inv.get('equipped', 'empty')}")

    items = inv.get("items", [])
    if items:
        lines.append("Inventory: " + ", ".join(f"{i['name']}x{i['count']}" for i in items[:15]))

    entities = env.get("nearby_entities", [])
    players = [e for e in entities if e.get("type") == "player"]
    hostiles = [e for e in entities if e.get("type") == "hostile"]
    if players:
        lines.append("Players: " + ", ".join(f"{p['name']}({p['distance']}m)" for p in players))
    if hostiles:
        lines.append("HOSTILES: " + ", ".join(f"{h['name']}({h['distance']}m)" for h in hostiles))

    blocks = env.get("nearby_objects", [])
    if blocks:
        lines.append("Blocks: " + ", ".join(b["name"] for b in blocks[:10]))

    return "\n".join(lines)


def format_event(event: dict) -> str:
    """Convert protocol event to text."""
    etype = event.get("event", "")
    data = event.get("data", {})
    if etype == "chat_received":
        w = " (whisper)" if data.get("whisper") else ""
        return f'[Chat{w}] {data.get("from","?")}: "{data.get("message","")}"'
    elif etype == "damage_taken":
        return f'[DAMAGE] from {data.get("source","?")}! HP:{int(data.get("health_remaining",1)*100)}%'
    elif etype == "death":
        return f'[DEATH] Cause: {data.get("cause","?")}'
    elif etype == "goal_reached":
        return f'[Done] {data.get("description","completed")}'
    elif etype == "goal_failed":
        return f'[Failed] {data.get("error","failed")}'
    else:
        return f"[{etype}] {json.dumps(data)}"


def parse_response(text: str) -> Optional[dict]:
    """Extract JSON from Claude's response."""
    text = text.strip()
    # Strip markdown fences
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[:text.rfind("```")]
    text = text.strip()

    # Find JSON object
    start = text.find("{")
    if start < 0:
        return None

    # Find balanced closing brace
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{": depth += 1
        elif text[i] == "}": depth -= 1
        if depth == 0:
            try:
                return json.loads(text[start:i+1])
            except json.JSONDecodeError:
                return None
    return None


class HermesBridge:
    def __init__(self):
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.client = anthropic.Anthropic(api_key=API_KEY)
        self.bodies: dict = {}
        self.last_call_time: float = 0
        self.processing: bool = False
        self.message_queue: asyncio.Queue = asyncio.Queue()
        self.last_state: dict = {}
        self.conversation: list = []  # Persistent conversation history

    async def connect(self):
        log.info("Connecting to hub at %s...", HUB_URL)
        self.ws = await websockets.connect(HUB_URL)
        await self.ws.send(json.dumps({"type": "orlo_connect"}))
        response = json.loads(await self.ws.recv())
        if response.get("type") == "bodies":
            for body in response.get("bodies", []):
                self.bodies[body["body_id"]] = body
                log.info("Body: %s (%s)", body.get("body_name"), body["body_id"])
        log.info("Connected. %d body(ies).", len(self.bodies))

    async def run(self):
        await self.connect()
        await asyncio.gather(
            self._receiver(),
            self._processor(),
            self._idle_loop(),
        )

    async def _receiver(self):
        async for raw in self.ws:
            await self.message_queue.put(json.loads(raw))

    async def _processor(self):
        while True:
            msg = await self.message_queue.get()
            msg_type = msg.get("type")
            body_id = msg.get("body_id")

            if msg_type == "state":
                self.last_state[body_id] = msg
                if time.time() - self.last_call_time < MIN_CALL_INTERVAL:
                    continue
                if not self.message_queue.empty():
                    continue  # Skip state if higher priority waiting
                await self._think(f"[State]\n{format_state(msg)}", body_id)

            elif msg_type == "event":
                event_name = msg.get("event", "")
                if event_name == "body_connected":
                    data = msg.get("data", {})
                    self.bodies[data["body_id"]] = data
                    log.info("Body connected: %s", data.get("body_name"))
                    continue

                text = format_event(msg)
                is_priority = event_name in ("damage_taken", "death", "chat_received")

                if is_priority:
                    # Wait for current processing to finish
                    for _ in range(40):  # max 20s wait
                        if not self.processing:
                            break
                        await asyncio.sleep(0.5)
                    state_ctx = ""
                    if body_id in self.last_state:
                        state_ctx = "\n" + format_state(self.last_state[body_id])
                    await self._think(f"{text}{state_ctx}", body_id)
                elif time.time() - self.last_call_time >= MIN_CALL_INTERVAL:
                    state_ctx = ""
                    if body_id in self.last_state:
                        state_ctx = "\n" + format_state(self.last_state[body_id])
                    await self._think(f"{text}{state_ctx}", body_id)

    async def _idle_loop(self):
        while True:
            await asyncio.sleep(IDLE_TIMEOUT)
            if not self.processing and self.last_state:
                body_id = next(iter(self.last_state))
                await self._think(
                    f"[Idle — what next?]\n{format_state(self.last_state[body_id])}",
                    body_id,
                )

    async def _think(self, context: str, body_id: str):
        if self.processing:
            return
        self.processing = True
        self.last_call_time = time.time()

        try:
            log.info("[in] %s", context[:120])

            # Add to conversation
            self.conversation.append({"role": "user", "content": context})

            # Trim history
            if len(self.conversation) > MAX_HISTORY:
                self.conversation = self.conversation[-MAX_HISTORY:]

            # Call Anthropic API directly
            t0 = time.time()
            response = await asyncio.to_thread(
                self.client.messages.create,
                model=MODEL,
                max_tokens=300,
                system=SYSTEM_PROMPT,
                messages=self.conversation,
            )
            elapsed = time.time() - t0

            reply_text = response.content[0].text
            log.info("[api] %.1fs | %s", elapsed, reply_text[:150])

            # Add to conversation history
            self.conversation.append({"role": "assistant", "content": reply_text})

            # Parse JSON response
            parsed = parse_response(reply_text)
            if not parsed:
                log.warning("[parse] Could not extract JSON: %s", reply_text[:200])
                return

            # Log thought
            thought = parsed.get("thought", "")
            if thought:
                log.info("[thought] %s", thought)

            # Send chat
            chat = parsed.get("chat")
            if chat:
                await self.ws.send(json.dumps({
                    "type": "action",
                    "action_id": f"chat-{int(time.time()*1000)}",
                    "body_id": body_id,
                    "action": "say",
                    "params": {"message": chat},
                }))

            # Send actions
            for i, act in enumerate(parsed.get("actions", [])):
                if not isinstance(act, dict) or "action" not in act:
                    continue
                if act["action"] == "say":
                    continue  # Already handled
                try:
                    await self.ws.send(json.dumps({
                        "type": "action",
                        "action_id": f"a-{int(time.time()*1000)}-{i}",
                        "body_id": body_id,
                        "action": act["action"],
                        "params": act.get("params", {}),
                    }))
                    log.info("[action] %s(%s)", act["action"], act.get("params", {}))
                except Exception as e:
                    log.error("[action err] %s", e)

        except Exception as e:
            log.error("[think err] %s", e)
        finally:
            self.processing = False


async def main():
    if not API_KEY:
        log.error("Set ANTHROPIC_API_KEY environment variable")
        return
    bridge = HermesBridge()
    while True:
        try:
            await bridge.run()
        except Exception as e:
            log.error("Bridge error: %s. Reconnecting in 5s...", e)
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
