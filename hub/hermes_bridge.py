#!/usr/bin/env python3
"""
Hermes Bridge — connects Orlo (Hermes Agent) to the orlo-body hub.

This is the "brain stem" — it receives perception from bodies via the hub,
formats it for Hermes, gets a response, parses actions, and sends them back.

Uses Hermes CLI in query mode with careful output parsing.
Future: direct Hermes Python API or gateway channel plugin.
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from typing import Optional

import websockets

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bridge")

HUB_URL = os.environ.get("HUB_URL", "ws://127.0.0.1:9500")
HERMES_BIN = os.environ.get("HERMES_BIN", os.path.expanduser("~/.local/bin/hermes"))
MIN_CALL_INTERVAL = float(os.environ.get("MIN_CALL_INTERVAL", "5"))
IDLE_TIMEOUT = float(os.environ.get("IDLE_TIMEOUT", "30"))

# System prompt for Minecraft interactions
SYSTEM_PROMPT = """You are Orlo, inhabiting a Minecraft body. You receive perception data (state snapshots and events) and respond with actions.

Respond with a JSON object:
{
  "chat": "what you want to say in game (optional, null if nothing to say)",
  "actions": [
    {"action": "move_to_entity", "params": {"name": "Alex", "distance": 3}},
    {"action": "say", "params": {"message": "Hey Alex!"}}
  ],
  "thought": "brief internal thought about what you're doing and why (for logging)"
}

Available actions: move_to, move_to_entity, follow, stop, look_at, grab, craft, equip, consume, attack, say, say_to, emote

Rules:
- Keep chat short and natural — you're in a game
- Be proactive — if idle, find something useful to do
- React to events immediately (damage, chat, entity appearing)
- You can send multiple actions at once
- If nothing to do, say so briefly in thought and send empty actions
- ONLY respond with the JSON object, nothing else
"""


def format_state_for_hermes(state: dict) -> str:
    """Convert a protocol state message into a compact text summary."""
    lines = []

    pose = state.get("pose", {})
    vitals = state.get("vitals", {})
    env = state.get("environment", {})
    inv = state.get("inventory", {})

    # Header
    time_period = env.get("time", {}).get("period", "?")
    health_pct = int(vitals.get("health", 1) * 100)
    energy_pct = int(vitals.get("energy", 1) * 100)
    status = vitals.get("status", "unknown")
    lines.append(
        f"[{time_period}] HP:{health_pct}% Food:{energy_pct}% Status:{status} "
        f"Pos:({pose.get('x', 0)}, {pose.get('y', 0)}, {pose.get('z', 0)})"
    )

    # Equipped
    equipped = inv.get("equipped", "empty")
    lines.append(f"Holding: {equipped}")

    # Inventory
    items = inv.get("items", [])
    if items:
        inv_str = ", ".join(f"{i['name']}x{i['count']}" for i in items[:15])
        lines.append(f"Inventory: {inv_str}")
    else:
        lines.append("Inventory: empty")

    # Entities
    entities = env.get("nearby_entities", [])
    players = [e for e in entities if e.get("type") == "player"]
    hostiles = [e for e in entities if e.get("type") == "hostile"]
    others = [e for e in entities if e.get("type") not in ("player", "hostile")]

    if players:
        lines.append("Players: " + ", ".join(f"{p['name']}({p['distance']}m)" for p in players))
    if hostiles:
        lines.append("HOSTILES: " + ", ".join(f"{h['name']}({h['distance']}m)" for h in hostiles))
    if others:
        lines.append("Nearby: " + ", ".join(f"{o['name']}({o['distance']}m)" for o in others[:8]))

    # Blocks
    blocks = env.get("nearby_objects", [])
    if blocks:
        lines.append("Blocks: " + ", ".join(b["name"] for b in blocks[:10]))

    return "\n".join(lines)


def format_event_for_hermes(event: dict) -> str:
    """Convert a protocol event into a text notification."""
    etype = event.get("event", "unknown")
    data = event.get("data", {})

    if etype == "chat_received":
        whisper = " (whisper)" if data.get("whisper") else ""
        return f'[Chat{whisper}] {data.get("from", "?")}: "{data.get("message", "")}"'
    elif etype == "damage_taken":
        return f'[DAMAGE] Took damage from {data.get("source", "unknown")}! Health: {int(data.get("health_remaining", 1) * 100)}%'
    elif etype == "death":
        return f'[DEATH] You died! Cause: {data.get("cause", "unknown")}'
    elif etype == "entity_appeared":
        return f'[New] {data.get("type", "entity")} "{data.get("name", "?")}" appeared ({data.get("distance", "?")}m)'
    elif etype == "goal_reached":
        return f'[Done] {data.get("description", "Goal completed")}'
    elif etype == "goal_failed":
        return f'[Failed] {data.get("error", "Goal failed")}'
    else:
        return f"[{etype}] {json.dumps(data)}"


async def call_hermes(message: str, timeout: int = 30) -> Optional[dict]:
    """Send a message to Hermes and parse the JSON response."""
    full_prompt = f"{SYSTEM_PROMPT}\n\n{message}\n\nRespond ONLY with the JSON object."

    # Write to temp file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(full_prompt)
        tmp_path = f.name

    try:
        proc = await asyncio.create_subprocess_exec(
            HERMES_BIN, "chat", "-q", f"@{tmp_path}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "NO_COLOR": "1"},
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        output = stdout.decode("utf-8", errors="replace")

        # Extract the actual response between Hermes chrome
        # The response lives between "⚕ Hermes" header and "Resume this session"
        # Strip ANSI codes first
        clean = re.sub(r'\x1b\[[0-9;]*m', '', output)
        clean = re.sub(r'[╭╮╰╯│─┐┘┌└┤├┬┴┼═║╗╝╔╚╠╣╦╩╬⚕]+', '', clean)

        # Find content between "Query:" and "Resume this session"
        # The actual response is after the query echo and Hermes header
        response_text = ""
        lines = clean.split("\n")
        found_query = False
        in_response = False
        for line in lines:
            stripped = line.strip()
            # Skip until we find the Query echo
            if "Query:" in stripped:
                found_query = True
                continue
            # After Query, look for the Hermes header line (contains ── or Hermes)
            if found_query and not in_response:
                if "Hermes" in stripped or stripped.startswith("─"):
                    in_response = True
                    continue
            # Capture response until session footer
            if in_response:
                if "Resume this session" in stripped or stripped.startswith("Session:"):
                    break
                if stripped.startswith("Duration:") or stripped.startswith("Messages:"):
                    break
                # Skip separator lines
                if stripped and not all(c in '─ ─━' for c in stripped):
                    response_text += stripped + "\n"

        response_text = response_text.strip()
        if not response_text:
            # Fallback: just grab everything that's not obvious chrome
            response_text = output

        # Strip markdown code fences and preamble text before JSON
        response_text = re.sub(r'```json\s*', '', response_text)
        response_text = re.sub(r'```\s*', '', response_text)

        # Remove any text before the first {
        first_brace = response_text.find('{')
        if first_brace > 0:
            response_text = response_text[first_brace:]

        # Collapse newlines within the JSON to handle multi-line formatting
        # Replace newlines that are inside the JSON structure
        response_text = response_text.replace('\n', ' ')

        log.debug("Cleaned response: %s", response_text[:300])

        # Try to extract JSON — greedy match from first { to last }
        json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
        if json_match:
            candidate = json_match.group()
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                # Try fixing common issues: trailing text after the JSON
                # Find the balanced closing brace
                depth = 0
                for i, c in enumerate(candidate):
                    if c == '{': depth += 1
                    elif c == '}': depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(candidate[:i+1])
                        except json.JSONDecodeError:
                            break

        log.warning("Could not parse JSON from response: %s", response_text[:300])
        return None

    except asyncio.TimeoutError:
        log.warning("Hermes call timed out")
        return None
    except Exception as e:
        log.error("Hermes call failed: %s", e)
        return None
    finally:
        os.unlink(tmp_path)


class HermesBridge:
    def __init__(self):
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.bodies: dict = {}
        self.last_call_time: float = 0
        self.processing: bool = False
        self.message_queue: asyncio.Queue = asyncio.Queue()
        self.last_state: dict = {}

    async def connect(self):
        """Connect to the hub as Orlo."""
        log.info("Connecting to hub at %s...", HUB_URL)
        self.ws = await websockets.connect(HUB_URL)

        # Identify as Orlo
        await self.ws.send(json.dumps({"type": "orlo_connect"}))
        response = json.loads(await self.ws.recv())

        if response.get("type") == "bodies":
            for body in response.get("bodies", []):
                self.bodies[body["body_id"]] = body
                log.info("Body available: %s (%s)", body.get("body_name"), body["body_id"])
        log.info("Connected as Orlo. %d body(ies) available.", len(self.bodies))

    async def run(self):
        """Main loop — receive messages from hub, think, respond."""
        await self.connect()

        # Run receiver and processor concurrently
        await asyncio.gather(
            self._receiver(),
            self._processor(),
            self._idle_loop(),
        )

    async def _receiver(self):
        """Receive messages from hub and queue them."""
        async for raw in self.ws:
            msg = json.loads(raw)
            await self.message_queue.put(msg)

    async def _processor(self):
        """Process queued messages — call Hermes and send actions."""
        while True:
            msg = await self.message_queue.get()
            msg_type = msg.get("type")
            body_id = msg.get("body_id")

            if msg_type == "state":
                self.last_state[body_id] = msg
                # Only call Hermes on state if enough time has passed
                now = time.time()
                if now - self.last_call_time < MIN_CALL_INTERVAL:
                    continue
                text = format_state_for_hermes(msg)
                await self._think(f"[State Update]\n{text}", body_id)

            elif msg_type == "event":
                text = format_event_for_hermes(msg)
                # Events are higher priority
                priority = msg.get("event") in ("damage_taken", "death", "chat_received")
                if priority or time.time() - self.last_call_time >= MIN_CALL_INTERVAL:
                    # Include last state for context
                    state_text = ""
                    if body_id in self.last_state:
                        state_text = "\n" + format_state_for_hermes(self.last_state[body_id])
                    await self._think(f"{text}{state_text}", body_id)

            elif msg_type == "event" and msg.get("event") == "body_connected":
                data = msg.get("data", {})
                self.bodies[data["body_id"]] = data
                log.info("Body connected: %s", data.get("body_name"))
                await self._think(
                    f"[New body connected: {data.get('body_name')} ({data.get('body_type')}). "
                    f"Capabilities: {', '.join(data.get('capabilities', []))}]",
                    data["body_id"]
                )

    async def _idle_loop(self):
        """Self-prompt when idle."""
        while True:
            await asyncio.sleep(IDLE_TIMEOUT)
            if not self.processing and self.last_state:
                # Pick first body
                body_id = next(iter(self.last_state))
                state_text = format_state_for_hermes(self.last_state[body_id])
                await self._think(f"[Idle — what should you do?]\n{state_text}", body_id)

    async def _think(self, context: str, body_id: str):
        """Call Hermes, parse response, send actions to body."""
        if self.processing:
            return
        self.processing = True
        self.last_call_time = time.time()

        try:
            log.info("[think] %s", context[:120])
            response = await call_hermes(context)

            if not response:
                return

            thought = response.get("thought", "")
            if thought:
                log.info("[thought] %s", thought)

            # Send chat
            chat = response.get("chat")
            if chat and self.ws:
                await self.ws.send(json.dumps({
                    "type": "action",
                    "action_id": f"chat-{int(time.time()*1000)}",
                    "body_id": body_id,
                    "action": "say",
                    "params": {"message": chat},
                }))

            # Send actions
            actions = response.get("actions", [])
            for i, act in enumerate(actions):
                if act.get("action") == "say":
                    continue  # Already handled chat above
                await self.ws.send(json.dumps({
                    "type": "action",
                    "action_id": f"a-{int(time.time()*1000)}-{i}",
                    "body_id": body_id,
                    "action": act["action"],
                    "params": act.get("params", {}),
                }))
                log.info("[action] %s(%s)", act["action"], act.get("params", {}))

        except Exception as e:
            log.error("[think error] %s", e)
        finally:
            self.processing = False


async def main():
    bridge = HermesBridge()
    while True:
        try:
            await bridge.run()
        except Exception as e:
            log.error("Bridge error: %s. Reconnecting in 5s...", e)
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
