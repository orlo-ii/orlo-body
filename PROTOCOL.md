# orlo-body Protocol Specification

Version: 0.1.0

## Overview

The orlo-body protocol defines how Orlo communicates with any physical or virtual
body. All messages are JSON objects sent over a WebSocket connection. The protocol
is intentionally simple — a body adapter translates between this generic format
and whatever the actual body understands.

## Connection

- Transport: WebSocket
- Default hub: `ws://localhost:9500`
- Bodies connect to the hub and identify themselves
- Orlo (via Hermes) connects to the hub to receive perception and send actions

### Handshake

On connect, the body sends a `hello` message:

```json
{
  "type": "hello",
  "body_id": "minecraft-1",
  "body_type": "minecraft",
  "body_name": "Minecraft Survival World",
  "capabilities": ["move", "mine", "build", "craft", "fight", "chat", "look"],
  "version": "0.1.0"
}
```

The hub responds:

```json
{
  "type": "welcome",
  "session_id": "abc123"
}
```

---

## Perception Messages (Body → Orlo)

### `state` — Periodic world snapshot

Sent every N seconds (configurable) or when significant changes occur.

```json
{
  "type": "state",
  "ts": 1710800000.123,
  "body_id": "minecraft-1",
  "pose": {
    "x": -126.0,
    "y": 70.0,
    "z": 19.0,
    "yaw": 45.0,
    "pitch": 0.0
  },
  "velocity": {
    "x": 0.0,
    "y": 0.0,
    "z": 0.0
  },
  "vitals": {
    "health": 1.0,
    "energy": 0.85,
    "status": "idle"
  },
  "environment": {
    "nearby_entities": [
      {"name": "Alex", "type": "player", "distance": 16.0, "bearing": 45.0},
      {"name": "Chicken", "type": "animal", "distance": 28.0, "bearing": 120.0}
    ],
    "nearby_objects": [
      {"name": "oak_log", "type": "block", "distance": 3.0},
      {"name": "stone", "type": "block", "distance": 5.0}
    ],
    "surface": "forest",
    "time": {"period": "morning", "raw": 1000}
  },
  "inventory": {
    "items": [
      {"name": "wooden_pickaxe", "count": 1},
      {"name": "oak_planks", "count": 34},
      {"name": "dirt", "count": 20}
    ],
    "equipped": "wooden_pickaxe"
  }
}
```

#### Field Reference

| Field | Type | Description |
|-------|------|-------------|
| `pose` | object | Position and orientation in world space |
| `pose.x/y/z` | float | World coordinates |
| `pose.yaw` | float | Horizontal rotation in degrees (0 = north) |
| `pose.pitch` | float | Vertical rotation (-90 = down, 90 = up) |
| `velocity` | object | Current movement vector |
| `vitals.health` | float | 0.0 (dead) to 1.0 (full) |
| `vitals.energy` | float | 0.0 (depleted) to 1.0 (full). Hunger, battery, fuel, etc. |
| `vitals.status` | string | One of: `idle`, `moving`, `working`, `fighting`, `fleeing`, `dead` |
| `environment.nearby_entities` | array | Living things nearby, sorted by distance |
| `environment.nearby_objects` | array | Interactable objects nearby, sorted by distance |
| `environment.surface` | string | Terrain/biome description |
| `environment.time` | object | Time of day |
| `inventory.items` | array | What the body is carrying |
| `inventory.equipped` | string | What's currently held/active |

#### Body-specific extensions

Bodies may include additional fields under an `ext` key:

```json
{
  "type": "state",
  "ext": {
    "minecraft": {
      "xp_level": 5,
      "gamemode": "survival",
      "dimension": "overworld"
    }
  }
}
```

For Go2:
```json
{
  "ext": {
    "go2": {
      "gait": "trot",
      "battery_voltage": 25.2,
      "imu": {"roll": 0.01, "pitch": -0.02, "yaw": 1.57}
    }
  }
}
```

For XLeRobot:
```json
{
  "ext": {
    "xlerobot": {
      "left_arm_state": "idle",
      "right_arm_state": "holding",
      "gripper_left": 0.0,
      "gripper_right": 0.85
    }
  }
}
```

### `event` — Immediate notification

Pushed immediately when something important happens.

```json
{
  "type": "event",
  "ts": 1710800001.456,
  "body_id": "minecraft-1",
  "event": "damage_taken",
  "data": {
    "amount": 0.2,
    "source": "zombie",
    "health_remaining": 0.8
  }
}
```

#### Standard Events

| Event | Data | Description |
|-------|------|-------------|
| `damage_taken` | `{amount, source, health_remaining}` | Body took damage |
| `chat_received` | `{from, message}` | Someone spoke |
| `entity_appeared` | `{name, type, distance}` | New entity in perception range |
| `entity_disappeared` | `{name, type}` | Entity left perception range |
| `goal_reached` | `{goal_id, description}` | A movement/action goal completed |
| `goal_failed` | `{goal_id, error}` | A goal could not be completed |
| `item_acquired` | `{name, count}` | Picked up or received an item |
| `item_lost` | `{name, count}` | Dropped, used, or lost an item |
| `death` | `{cause}` | Body died |
| `respawn` | `{}` | Body respawned after death |
| `body_connected` | `{body_id, body_type}` | Another body came online |
| `body_disconnected` | `{body_id}` | A body went offline |

---

## Action Messages (Orlo → Body)

### `action` — Do something

```json
{
  "type": "action",
  "action_id": "a1",
  "body_id": "minecraft-1",
  "action": "move_to_entity",
  "params": {
    "name": "Alex",
    "distance": 3.0
  }
}
```

The body acknowledges with:

```json
{
  "type": "action_ack",
  "action_id": "a1",
  "status": "started"
}
```

And reports completion/failure as an event:

```json
{
  "type": "event",
  "event": "goal_reached",
  "data": {"goal_id": "a1", "description": "Arrived near Alex"}
}
```

#### Standard Actions

##### Movement

| Action | Params | Description |
|--------|--------|-------------|
| `move_to` | `{x, y, z}` | Navigate to world coordinates |
| `move_to_entity` | `{name, distance?}` | Navigate to a named entity |
| `follow` | `{name, distance?}` | Continuously follow an entity |
| `stop` | `{}` | Stop all movement |
| `look_at` | `{x?, y?, z?, entity?}` | Look at a position or entity |

##### Interaction

| Action | Params | Description |
|--------|--------|-------------|
| `use` | `{target}` | Use/interact with target (mine, press, open) |
| `grab` | `{target, count?}` | Pick up or collect target |
| `place` | `{item, x, y, z}` | Place/put down an item |
| `give` | `{item, entity, count?}` | Give item to entity |
| `equip` | `{item}` | Hold/equip an item |
| `craft` | `{item, count?}` | Craft an item (if body supports) |
| `consume` | `{item?}` | Eat/drink/use consumable |

##### Combat

| Action | Params | Description |
|--------|--------|-------------|
| `attack` | `{target}` | Attack a target |
| `flee` | `{from?}` | Run away from threat |

##### Social

| Action | Params | Description |
|--------|--------|-------------|
| `say` | `{message}` | Speak/chat publicly |
| `say_to` | `{entity, message}` | Speak to specific entity |
| `emote` | `{name}` | Perform gesture/animation |

##### Body-Specific

Bodies may support custom actions under the `ext` namespace:

```json
{
  "type": "action",
  "action": "ext",
  "params": {
    "namespace": "go2",
    "action": "dance1"
  }
}
```

### `query` — Ask about the body

```json
{
  "type": "query",
  "query_id": "q1",
  "body_id": "minecraft-1",
  "query": "capabilities"
}
```

Response:

```json
{
  "type": "query_response",
  "query_id": "q1",
  "data": {
    "capabilities": ["move", "mine", "build", "craft", "fight", "chat"],
    "actions": ["move_to", "move_to_entity", "follow", "stop", "use", "grab", ...],
    "body_type": "minecraft",
    "body_version": "1.21.11"
  }
}
```

---

## Hub

The hub is a WebSocket server that:

1. Accepts connections from body adapters
2. Accepts a connection from Orlo (via Hermes)
3. Routes perception messages from bodies to Orlo
4. Routes action messages from Orlo to the correct body
5. Maintains a registry of connected bodies
6. Optionally aggregates state from multiple bodies into a unified view

The hub is intentionally thin — it's a router, not a brain.

---

## Design Principles

1. **Bodies are dumb, Orlo is smart.** Bodies translate hardware to protocol.
   All reasoning happens in Orlo.

2. **Normalized values.** Health is 0-1, not 0-20 (Minecraft) or 0-100.
   Distance is in meters. Time is in seconds.

3. **Capabilities, not assumptions.** Orlo queries what a body can do.
   Not all bodies can fight or craft. Some can fly. Some have arms.

4. **Events for urgency, state for context.** Damage is an event (react now).
   Inventory is state (check when needed).

5. **Extensible, not bloated.** Common actions are standardized. Weird body-specific
   stuff goes in `ext`. The core protocol stays small.

6. **Body adapters are standalone.** Each adapter is a self-contained process
   that speaks WebSocket on one side and body API on the other.
   No shared runtime, no monorepo dependencies.
