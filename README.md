# orlo-body

Unified perception/action protocol for Orlo across multiple bodies.

One agent, many bodies. Whether it's a quadruped robot, a dual-arm household bot,
or a Minecraft character — the interface to Orlo is the same.

## Architecture

```
                    ┌─────────────────────┐
                    │       Orlo          │
                    │  (Hermes Agent)     │
                    │                     │
                    │  memory, reasoning, │
                    │  personality, goals │
                    └────────┬────────────┘
                             │
                     WebSocket (JSON)
                     orlo-body protocol
                             │
              ┌──────────────┼──────────────┐
              │              │              │
     ┌────────┴───┐  ┌──────┴─────┐  ┌────┴────────┐
     │  Minecraft  │  │  Unitree   │  │  XLeRobot   │
     │  Adapter    │  │  Go2       │  │  Adapter    │
     │             │  │  Adapter   │  │             │
     │ mineflayer  │  │ DimOS MCP  │  │ lerobot SDK │
     └─────────────┘  └────────────┘  └─────────────┘
```

## Protocol

All communication is JSON over WebSocket. Two message types flow in each direction:

**Body → Orlo (perception):**
- `state` — periodic world state snapshot
- `event` — immediate notification (damage, chat, goal reached)

**Orlo → Body (action):**
- `action` — do something (move, grab, say, attack)
- `query` — ask about something (capabilities, detailed inspection)

See [PROTOCOL.md](PROTOCOL.md) for the full specification.

## Bodies

| Body | Type | Transport | Status |
|------|------|-----------|--------|
| Minecraft | Virtual (Java Edition) | mineflayer | 🟡 In progress |
| Unitree Go2 | Quadruped robot | DimOS MCP | 📋 Planned |
| XLeRobot | Dual-arm mobile | lerobot SDK | 📋 Planned |

## Structure

```
orlo-body/
├── PROTOCOL.md           # Full protocol specification
├── schema/               # JSON schemas for messages
│   ├── perception.json
│   └── action.json
├── hub/                  # WebSocket hub (connects Orlo to bodies)
│   └── server.py
├── adapters/
│   ├── minecraft/        # Minecraft ↔ protocol (Node.js)
│   ├── go2/              # Unitree Go2 ↔ protocol (Python)
│   └── xlerobot/         # XLeRobot ↔ protocol (Python)
└── examples/
    └── echo_body.py      # Minimal test body for development
```

## Quick Start

```bash
# Start the hub
python hub/server.py

# In another terminal, start a body adapter
cd adapters/minecraft && node adapter.js

# Orlo connects via Hermes and starts perceiving/acting
```
