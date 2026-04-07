#!/usr/bin/env python3
"""Fix private_ws.py: add exponential backoff to all 3 exchange reconnect loops."""
import re

with open("arbitrage/exchanges/private_ws.py", "r") as f:
    lines = f.readlines()

output = []
i = 0
while i < len(lines):
    line = lines[i]

    # Pattern 1: Add _reconnect_attempt after 'self.running = False' in __init__
    # Only for sections that DON'T already have it
    if "self.running = False" in line and i + 1 < len(lines):
        next_line = lines[i + 1]
        if "_reconnect_attempt" not in next_line:
            output.append(line)
            output.append("        self._reconnect_attempt: int = 0\n")
            i += 1
            continue

    # Pattern 2: Replace fixed sleep in ConnectionClosed handler
    # e.g.: "                    await asyncio.sleep(2)\n" after ConnectionClosed
    if "await asyncio.sleep(" in line and "if self.running:" in lines[i - 1]:
        # Check context: is this after ConnectionClosed or generic Exception?
        # Look back for "except" line
        for j in range(i - 1, max(i - 5, 0), -1):
            if "except" in lines[j]:
                if "ConnectionClosed" in lines[j] or "Exception" in lines[j]:
                    indent = line[:len(line) - len(line.lstrip())]
                    output.append(f"{indent}self._reconnect_attempt += 1\n")
                    output.append(f"{indent}delay = min(2 ** self._reconnect_attempt, 30)\n")
                    output.append(f"{indent}await asyncio.sleep(delay)\n")
                    i += 1
                    continue
                break

    # Pattern 3: Add reconnect_attempt reset after successful connection
    # Look for "authenticated" or "subscribed" log lines followed by async for
    if "self._reconnect_attempt = 0" not in line and "async for message in ws:" in line:
        # Insert reconnect_attempt reset before this line
        indent = line[:len(line) - len(line.lstrip())]
        output.append(f"{indent}self._reconnect_attempt = 0\n")

    output.append(line)
    i += 1

with open("arbitrage/exchanges/private_ws.py", "w") as f:
    f.writelines(output)

print(f"Done: wrote {len(output)} lines")
