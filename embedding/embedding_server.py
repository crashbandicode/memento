"""Standalone embedding HTTP server — runs on host machine, called by API container.

Usage:
  python -m server.services.embedding_server [--port 8002] [--model BAAI/bge-m3]

Provides a single endpoint:
  POST /embed  {"texts": ["hello", "world"]}  →  {"embeddings": [[...], [...]]}
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import socket
from http.server import HTTPServer, BaseHTTPRequestHandler


class DualStackHTTPServer(HTTPServer):
    """Listen on both IPv4 and IPv6. Without this, the default HTTPServer
    binds AF_INET only, but docker DNS for a service alias returns AAAA
    records first — clients that follow RFC 6555 happy-eyeballs spend
    seconds timing out the IPv6 attempt before falling back to IPv4.
    Binding to `::` with IPV6_V6ONLY=0 means the same socket accepts
    both v4 and v6 traffic, no client-side workaround needed."""
    address_family = socket.AF_INET6

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("embedding_server")

_model = None


def _load_model(model_name: str):
    global _model
    logger.info("Loading %s ...", model_name)
    from sentence_transformers import SentenceTransformer
    _model = SentenceTransformer(model_name)
    logger.info("Model loaded: %s (dim=%d)", model_name, _model.get_sentence_embedding_dimension())


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/embed":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            texts = body.get("texts", [])
            if not texts:
                self._json_response({"embeddings": []})
                return

            embeddings = _model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
            self._json_response({"embeddings": [e.tolist() for e in embeddings]})
        except Exception as e:
            logger.error("Error: %s", e)
            self.send_error(500, str(e))

    def do_GET(self):
        if self.path == "/health":
            self._json_response({"status": "ok", "model": _model is not None})
        else:
            self.send_error(404)

    def _json_response(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        logger.info(format, *args)


def main():
    parser = argparse.ArgumentParser(description="Embedding HTTP Server")
    parser.add_argument("--port", type=int, default=int(os.environ.get("MEMENTO_EMBEDDING_PORT", "8002")))
    parser.add_argument("--model", default=os.environ.get("MEMENTO_EMBEDDING_MODEL_NAME", "BAAI/bge-m3"))
    args = parser.parse_args()

    _load_model(args.model)

    DualStackHTTPServer.allow_reuse_address = True
    server = DualStackHTTPServer(("::", args.port), Handler)
    logger.info("Embedding server running on port %d (dual-stack)", args.port)
    server.serve_forever()


if __name__ == "__main__":
    main()
