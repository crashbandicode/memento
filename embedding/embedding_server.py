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
import socket
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn


class DualStackHTTPServer(ThreadingMixIn, HTTPServer):
    """Listen on both IPv4 and IPv6. Without this, the default HTTPServer
    binds AF_INET only, but docker DNS for a service alias returns AAAA
    records first — clients that follow RFC 6555 happy-eyeballs spend
    seconds timing out the IPv6 attempt before falling back to IPv4.
    Binding to `::` with IPV6_V6ONLY=0 means the same socket accepts
    both v4 and v6 traffic, no client-side workaround needed."""

    address_family = socket.AF_INET6
    daemon_threads = True

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("embedding_server")

_model = None
_encode_lock = threading.Lock()
_embed_slots = threading.BoundedSemaphore(
    int(os.environ.get("MEMENTO_EMBEDDING_MAX_QUEUED_REQUESTS", "8"))
)
_max_request_bytes = int(
    os.environ.get("MEMENTO_EMBEDDING_MAX_REQUEST_BYTES", str(8 * 1024 * 1024))
)


def _load_model(model_name: str):
    global _model
    logger.info("Loading %s ...", model_name)
    from sentence_transformers import SentenceTransformer

    _model = SentenceTransformer(model_name)
    logger.info(
        "Model loaded: %s (dim=%d)",
        model_name,
        _model.get_sentence_embedding_dimension(),
    )


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/embed":
            self._safe_error(404)
            return
        if not _embed_slots.acquire(blocking=False):
            self._safe_error(503, "embedding queue is full")
            return
        try:
            try:
                length = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                self._safe_error(400, "invalid Content-Length")
                return
            if length < 0 or length > _max_request_bytes:
                self._safe_error(413, "embedding request is too large")
                return
            try:
                body = json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                self._safe_error(400, "invalid JSON body")
                return
            texts = body.get("texts", [])
            if not texts:
                self._json_response({"embeddings": []})
                return

            # SentenceTransformer inference remains serialized, while the
            # threaded HTTP server can still answer /health during a long run.
            with _encode_lock:
                embeddings = _model.encode(
                    texts,
                    normalize_embeddings=True,
                    show_progress_bar=False,
                )
            self._json_response({"embeddings": [e.tolist() for e in embeddings]})
        except ConnectionError:
            logger.info("Embedding client disconnected before the response completed")
        except Exception as e:
            logger.error("Error: %s", e)
            self._safe_error(500, str(e))
        finally:
            _embed_slots.release()

    def do_GET(self):
        if self.path == "/health":
            self._json_response({"status": "ok", "model": _model is not None})
        else:
            self._safe_error(404)

    def _safe_error(self, code, message=None):
        try:
            self.send_error(code, message)
        except (BrokenPipeError, ConnectionResetError, OSError):
            logger.info("HTTP client disconnected before error response completed")

    def _json_response(self, data):
        body = json.dumps(data).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            logger.info("HTTP client disconnected before the response completed")

    def log_message(self, format, *args):
        logger.info(format, *args)


def main():
    parser = argparse.ArgumentParser(description="Embedding HTTP Server")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MEMENTO_EMBEDDING_PORT", "8002")),
    )
    parser.add_argument(
        "--model", default=os.environ.get("MEMENTO_EMBEDDING_MODEL_NAME", "BAAI/bge-m3")
    )
    args = parser.parse_args()

    _load_model(args.model)

    DualStackHTTPServer.allow_reuse_address = True
    server = DualStackHTTPServer(("::", args.port), Handler)
    logger.info("Embedding server running on port %d (dual-stack)", args.port)
    server.serve_forever()


if __name__ == "__main__":
    main()
