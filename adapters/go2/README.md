# Unitree Go2 Adapter

Translates between the Unitree Go2 (via DimOS MCP) and the orlo-body protocol.

## Status: Planned

Blocked on Go2 connectivity (WiFi dead, attempting BLE fix / ethernet).
DimOS MCP server is configured and ready on Mac mini.

## Architecture
```
Go2 hardware ↔ DimOS (WebRTC) ↔ DimOS MCP (HTTP :9990) ↔ Go2 Adapter ↔ Hub
```

## Dependencies
- Python 3.10+
- DimOS running with unitree-go2-agentic-mcp blueprint
- websockets
