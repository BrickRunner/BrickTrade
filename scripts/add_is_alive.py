#!/usr/bin/env python3
"""Add is_alive() method to all WS client classes."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

METHOD_TEXT = (
    "    def is_alive(self) -> bool:\n"
    "        \"\"\"Check if WS connection is active.\"\"\"\n"
    "        return self._running if hasattr(self, \"_running\") else False\n"
    "\n"
)

files_to_fix = [
    "arbitrage/exchanges/binance_ws.py",
    "arbitrage/exchanges/bybit_ws.py",
    "arbitrage/exchanges/okx_ws.py",
    "arbitrage/exchanges/htx_ws.py",
]

base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

for rel_path in files_to_fix:
    full = os.path.join(base, rel_path)
    with open(full, "r") as f:
        content = f.read()
    if "def is_alive" in content:
        print(f"  is_alive already in {rel_path}")
        continue
    lines = content.split("\n")
    for i, line in enumerate(lines):
        if "class " in line and "WebSocket" in line:
            # Find next async def or def after class
            for j in range(i + 1, len(lines)):
                stripped = lines[j].strip()
                if stripped.startswith("async def ") or stripped.startswith("def "):
                    lines.insert(j, METHOD_TEXT.rstrip("\n"))
                    with open(full, "w") as f:
                        f.write("\n".join(lines))
                    print(f"  Added is_alive to {rel_path}")
                    break
            break