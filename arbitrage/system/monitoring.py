from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from collections import deque
from dataclasses import dataclass, field
from typing import Dict


@dataclass
class InMemoryMonitoring:
    logger: logging.Logger
    max_events: int = 1000
    events: deque = field(init=False)
    _metrics: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.events = deque(maxlen=self.max_events)

    async def emit(self, event: str, payload: Dict) -> None:
        record = {"event": event, "payload": payload}
        self.events.append(record)
        self._metrics[event] = self._metrics.get(event, 0) + 1
        self.logger.info("%s %s", event, payload)

    def metrics_text(self) -> str:
        lines = []
        for key, val in sorted(self._metrics.items()):
            metric = key.replace(".", "_").replace("-", "_")
            lines.append(f"event_{metric}_total {val}")
        return "\n".join(lines) + "\n"

    def start_metrics_server(self, host: str = "127.0.0.1", port: int = 9109) -> None:
        monitor = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path != "/metrics":
                    self.send_response(404)
                    self.end_headers()
                    return
                payload = monitor.metrics_text().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format, *args):  # noqa: A003
                return

        server = HTTPServer((host, port), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
