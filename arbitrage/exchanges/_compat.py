"""
Compatibility helpers for websockets library version differences.

websockets <=12: WebSocketClientProtocol has a `.open` boolean property.
websockets >=13: ClientConnection removed `.open`; use `.state` instead.
"""
from __future__ import annotations


def ws_is_open(ws) -> bool:
    """Check whether a websocket connection is open (works with any version)."""
    # websockets <=12: .open exists
    if hasattr(ws, "open"):
        return bool(ws.open)
    # websockets >=13 (ClientConnection): check .state
    try:
        return ws.state.name == "OPEN"
    except Exception:
        return False
