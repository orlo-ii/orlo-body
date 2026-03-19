#!/usr/bin/env node
/**
 * Minecraft Adapter — bridges mineflayer to the orlo-body protocol.
 *
 * Connects to:
 *   1. Minecraft server (via mineflayer)
 *   2. orlo-body hub (via WebSocket)
 *
 * Sends perception (state + events) to hub.
 * Receives actions from hub and executes via mineflayer.
 */

const mineflayer = require('mineflayer');
const pathfinder = require('mineflayer-pathfinder').pathfinder;
const { Movements, goals: { GoalNear, GoalFollow } } = require('mineflayer-pathfinder');
const collectBlock = require('mineflayer-collectblock').plugin;
const WebSocket = require('ws');
const minecraftData = require('minecraft-data');

// ── Config ──────────────────────────────────────────────────────────────
const HUB_URL = process.env.HUB_URL || 'ws://127.0.0.1:9500';
const MC_HOST = process.env.MC_HOST || '127.0.0.1';
const MC_PORT = parseInt(process.env.MC_PORT || '0'); // 0 = auto-detect
const MC_USERNAME = process.env.MC_USERNAME || 'Orlo';
const BODY_ID = process.env.BODY_ID || 'minecraft-1';
const STATE_INTERVAL = parseInt(process.env.STATE_INTERVAL || '10000');

let bot, hub, mcData;
let lastStateSent = '';

// ── LAN Port Detection ─────────────────────────────────────────────────
function findLanPort() {
  const dgram = require('dgram');
  return new Promise((resolve, reject) => {
    const socket = dgram.createSocket({ type: 'udp4', reuseAddr: true });
    const timeout = setTimeout(() => { socket.close(); reject(new Error('No LAN game found')); }, 10000);
    socket.on('message', (msg) => {
      const match = msg.toString().match(/\[MOTD\].*\[\/MOTD\]\[AD\](\d+)\[\/AD\]/);
      if (match) { clearTimeout(timeout); socket.close(); resolve(parseInt(match[1])); }
    });
    socket.bind(4445, () => { socket.addMembership('224.0.2.60'); });
  });
}

// ═══════════════════════════════════════════════════════════════════════
// PERCEPTION — extract game state into protocol format
// ═══════════════════════════════════════════════════════════════════════

function buildState() {
  const pos = bot.entity.position;
  const time = bot.time.timeOfDay;
  let period = 'morning';
  if (time >= 6000 && time < 12000) period = 'afternoon';
  else if (time >= 12000 && time < 13000) period = 'sunset';
  else if (time >= 13000 && time < 23000) period = 'night';
  else if (time >= 23000) period = 'sunrise';

  // Nearby entities
  const entities = [];
  for (const entity of Object.values(bot.entities)) {
    if (entity === bot.entity) continue;
    const dist = entity.position.distanceTo(pos);
    if (dist > 32) continue;
    const name = entity.username || entity.displayName || entity.name || entity.type;
    const type = entity.type === 'player' ? 'player' :
                 (entity.type === 'mob' ? 'hostile' : entity.type);
    const dx = entity.position.x - pos.x;
    const dz = entity.position.z - pos.z;
    const bearing = (Math.atan2(dx, dz) * 180 / Math.PI + 360) % 360;
    entities.push({ name, type, distance: Math.round(dist * 10) / 10, bearing: Math.round(bearing) });
  }
  entities.sort((a, b) => a.distance - b.distance);

  // Nearby blocks (sampled)
  const blockCounts = {};
  for (let dx = -16; dx <= 16; dx += 4) {
    for (let dy = -8; dy <= 8; dy += 4) {
      for (let dz = -16; dz <= 16; dz += 4) {
        const block = bot.blockAt(pos.offset(dx, dy, dz));
        if (block && block.name !== 'air' && block.name !== 'cave_air') {
          blockCounts[block.name] = (blockCounts[block.name] || 0) + 1;
        }
      }
    }
  }
  const nearbyBlocks = Object.entries(blockCounts)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 12)
    .map(([name]) => ({ name, type: 'block', distance: 8.0 }));

  // Inventory
  const items = bot.inventory.items().map(i => ({ name: i.name, count: i.count }));
  const grouped = {};
  for (const i of items) grouped[i.name] = (grouped[i.name] || 0) + i.count;
  const inventory = Object.entries(grouped).map(([name, count]) => ({ name, count }));
  const hand = bot.heldItem;

  return {
    type: 'state',
    ts: Date.now() / 1000,
    body_id: BODY_ID,
    pose: {
      x: Math.round(pos.x * 10) / 10,
      y: Math.round(pos.y * 10) / 10,
      z: Math.round(pos.z * 10) / 10,
      yaw: Math.round(bot.entity.yaw * 180 / Math.PI),
      pitch: Math.round(bot.entity.pitch * 180 / Math.PI),
    },
    velocity: {
      x: Math.round((bot.entity.velocity?.x || 0) * 100) / 100,
      y: Math.round((bot.entity.velocity?.y || 0) * 100) / 100,
      z: Math.round((bot.entity.velocity?.z || 0) * 100) / 100,
    },
    vitals: {
      health: Math.round(bot.health / 20 * 100) / 100,
      energy: Math.round(bot.food / 20 * 100) / 100,
      status: bot.pathfinder?.isMoving() ? 'moving' : 'idle',
    },
    environment: {
      nearby_entities: entities.slice(0, 15),
      nearby_objects: nearbyBlocks,
      surface: bot.game?.dimension || 'overworld',
      time: { period, raw: time },
    },
    inventory: {
      items: inventory,
      equipped: hand ? hand.name : 'empty',
    },
    ext: {
      minecraft: {
        xp_level: bot.experience?.level || 0,
        gamemode: bot.game?.gameMode || 'survival',
        dimension: bot.game?.dimension || 'overworld',
      },
    },
  };
}

function sendEvent(event, data) {
  if (hub?.readyState === WebSocket.OPEN) {
    hub.send(JSON.stringify({
      type: 'event',
      ts: Date.now() / 1000,
      body_id: BODY_ID,
      event,
      data,
    }));
  }
}

// ═══════════════════════════════════════════════════════════════════════
// ACTIONS — execute protocol actions via mineflayer
// ═══════════════════════════════════════════════════════════════════════

async function executeAction(msg) {
  const { action, params = {}, action_id } = msg;
  const ack = (status) => hub.send(JSON.stringify({ type: 'action_ack', action_id, status }));

  try {
    switch (action) {
      // Movement
      case 'move_to': {
        const movements = new Movements(bot);
        bot.pathfinder.setMovements(movements);
        bot.pathfinder.setGoal(new GoalNear(params.x, params.y, params.z, 2));
        ack('started');
        break;
      }
      case 'move_to_entity': {
        const player = bot.players[params.name];
        if (!player?.entity) { ack('error: entity not found'); break; }
        const movements = new Movements(bot);
        bot.pathfinder.setMovements(movements);
        bot.pathfinder.setGoal(new GoalNear(
          player.entity.position.x, player.entity.position.y, player.entity.position.z,
          params.distance || 3
        ));
        ack('started');
        break;
      }
      case 'follow': {
        const target = bot.players[params.name];
        if (!target?.entity) { ack('error: entity not found'); break; }
        const movements = new Movements(bot);
        bot.pathfinder.setMovements(movements);
        bot.pathfinder.setGoal(new GoalFollow(target.entity, params.distance || 4), true);
        ack('started');
        break;
      }
      case 'stop': {
        bot.pathfinder.setGoal(null);
        bot.stopDigging();
        ack('completed');
        break;
      }
      case 'look_at': {
        if (params.entity) {
          const p = bot.players[params.entity];
          if (p?.entity) await bot.lookAt(p.entity.position.offset(0, 1.6, 0));
        } else if (params.x !== undefined) {
          const { Vec3 } = require('vec3');
          await bot.lookAt(new Vec3(params.x, params.y, params.z));
        }
        ack('completed');
        break;
      }

      // Interaction
      case 'grab': {
        if (bot.collectBlock && mcData) {
          const blockId = mcData.blocksByName[params.target];
          if (!blockId) { ack('error: unknown block'); break; }
          const blocks = bot.findBlocks({ matching: blockId.id, maxDistance: 64, count: params.count || 10 });
          const targets = blocks.map(pos => bot.blockAt(pos)).filter(Boolean);
          ack('started');
          await bot.collectBlock.collect(targets.slice(0, params.count || 10));
          sendEvent('goal_reached', { goal_id: action_id, description: `Collected ${params.target}` });
        }
        break;
      }
      case 'craft': {
        const itemId = mcData.itemsByName[params.item]?.id;
        if (!itemId) { ack('error: unknown item'); break; }
        const recipe = bot.recipesFor(itemId)?.[0];
        if (!recipe) { ack('error: no recipe or missing ingredients'); break; }
        const table = bot.findBlock({ matching: mcData.blocksByName['crafting_table']?.id, maxDistance: 32 });
        ack('started');
        await bot.craft(recipe, params.count || 1, table || undefined);
        sendEvent('goal_reached', { goal_id: action_id, description: `Crafted ${params.item}` });
        break;
      }
      case 'equip': {
        const item = bot.inventory.items().find(i => i.name === params.item);
        if (!item) { ack('error: item not in inventory'); break; }
        await bot.equip(item, 'hand');
        ack('completed');
        break;
      }
      case 'consume': {
        const food = bot.inventory.items().find(i =>
          i.name.includes('cooked') || i.name.includes('bread') ||
          i.name.includes('apple') || i.name.includes('steak')
        );
        if (!food) { ack('error: no food'); break; }
        await bot.equip(food, 'hand');
        await bot.consume();
        ack('completed');
        break;
      }

      // Combat
      case 'attack': {
        let target = null;
        for (const entity of Object.values(bot.entities)) {
          if (entity === bot.entity) continue;
          const eName = entity.username || entity.displayName || entity.name || '';
          if (eName.toLowerCase().includes(params.target.toLowerCase())) { target = entity; break; }
        }
        if (!target) { ack('error: target not found'); break; }
        await bot.attack(target);
        ack('completed');
        break;
      }

      // Social
      case 'say': {
        bot.chat(params.message);
        ack('completed');
        break;
      }
      case 'say_to': {
        bot.whisper(params.entity, params.message);
        ack('completed');
        break;
      }
      case 'emote': {
        // Minecraft doesn't have real emotes, just chat
        bot.chat(`* ${MC_USERNAME} ${params.name || 'waves'}`);
        ack('completed');
        break;
      }

      default:
        ack(`error: unknown action '${action}'`);
    }
  } catch (err) {
    console.error(`[action error] ${action}:`, err.message);
    ack(`error: ${err.message}`);
  }
}

// ═══════════════════════════════════════════════════════════════════════
// MAIN — connect to both Minecraft and the hub
// ═══════════════════════════════════════════════════════════════════════

async function main() {
  // Auto-detect LAN port
  let port = MC_PORT;
  if (!port) {
    console.log('[mc] Scanning for LAN game...');
    port = await findLanPort();
    console.log(`[mc] Found LAN game on port ${port}`);
  }

  // Connect to Minecraft
  console.log(`[mc] Connecting to ${MC_HOST}:${port} as ${MC_USERNAME}...`);
  bot = mineflayer.createBot({
    host: MC_HOST, port, username: MC_USERNAME, auth: 'offline',
  });
  bot.loadPlugin(pathfinder);
  bot.loadPlugin(collectBlock);

  // Wait for spawn
  await new Promise((resolve, reject) => {
    bot.once('spawn', resolve);
    bot.once('error', reject);
    bot.once('kicked', (reason) => reject(new Error(`Kicked: ${reason}`)));
  });
  mcData = minecraftData(bot.version);
  console.log(`[mc] Spawned! Minecraft ${bot.version}`);

  // Connect to hub
  console.log(`[hub] Connecting to ${HUB_URL}...`);
  hub = new WebSocket(HUB_URL);

  await new Promise((resolve, reject) => {
    hub.on('open', () => {
      // Send hello
      hub.send(JSON.stringify({
        type: 'hello',
        body_id: BODY_ID,
        body_type: 'minecraft',
        body_name: `Minecraft ${bot.version} (${MC_USERNAME})`,
        capabilities: ['move', 'mine', 'build', 'craft', 'fight', 'chat', 'look'],
        version: '0.1.0',
      }));
      resolve();
    });
    hub.on('error', reject);
  });
  console.log('[hub] Connected');

  // Handle messages from hub (actions from Orlo)
  hub.on('message', async (raw) => {
    try {
      const msg = JSON.parse(raw);
      if (msg.type === 'welcome') {
        console.log(`[hub] Session: ${msg.session_id}`);
      } else if (msg.type === 'action') {
        console.log(`[action] ${msg.action}(${JSON.stringify(msg.params || {})})`);
        await executeAction(msg);
      } else if (msg.type === 'query' && msg.query === 'capabilities') {
        hub.send(JSON.stringify({
          type: 'query_response',
          query_id: msg.query_id,
          data: {
            capabilities: ['move', 'mine', 'build', 'craft', 'fight', 'chat', 'look'],
            actions: ['move_to', 'move_to_entity', 'follow', 'stop', 'look_at',
                      'grab', 'craft', 'equip', 'consume', 'attack', 'say', 'say_to', 'emote'],
            body_type: 'minecraft',
            body_version: bot.version,
          },
        }));
      }
    } catch (err) {
      console.error('[hub] Message error:', err.message);
    }
  });

  // ── Periodic state ────────────────────────────────────────────────
  setInterval(() => {
    if (hub?.readyState === WebSocket.OPEN) {
      const state = buildState();
      const stateStr = JSON.stringify(state);
      if (stateStr !== lastStateSent) {
        hub.send(stateStr);
        lastStateSent = stateStr;
      }
    }
  }, STATE_INTERVAL);

  // Send initial state immediately
  setTimeout(() => {
    if (hub?.readyState === WebSocket.OPEN) {
      hub.send(JSON.stringify(buildState()));
    }
  }, 1000);

  // ── Game events → protocol events ─────────────────────────────────
  bot.on('chat', (username, message) => {
    if (username === bot.username) return;
    sendEvent('chat_received', { from: username, message });
  });

  bot.on('whisper', (username, message) => {
    if (username === bot.username) return;
    sendEvent('chat_received', { from: username, message, whisper: true });
  });

  bot.on('health', () => {
    if (bot.health < 10) {
      sendEvent('damage_taken', {
        amount: Math.round((1 - bot.health / 20) * 100) / 100,
        source: 'unknown',
        health_remaining: Math.round(bot.health / 20 * 100) / 100,
      });
    }
  });

  bot.on('death', () => sendEvent('death', { cause: 'unknown' }));
  bot.on('respawn', () => sendEvent('respawn', {}));

  bot.on('playerJoined', (player) => {
    if (player.username !== bot.username) {
      sendEvent('entity_appeared', { name: player.username, type: 'player', distance: 0 });
    }
  });

  bot.on('playerLeft', (player) => {
    sendEvent('entity_disappeared', { name: player.username, type: 'player' });
  });

  bot.on('kicked', (reason) => { console.log('[mc] Kicked:', reason); process.exit(1); });
  bot.on('error', (err) => { console.error('[mc] Error:', err.message); });
  bot.on('end', () => { console.log('[mc] Disconnected'); process.exit(0); });
  hub.on('close', () => { console.log('[hub] Disconnected'); process.exit(1); });

  console.log('[adapter] Minecraft adapter running. Sending state every', STATE_INTERVAL, 'ms');
}

main().catch(err => { console.error('[fatal]', err); process.exit(1); });
