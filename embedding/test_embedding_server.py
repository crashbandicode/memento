from __future__ import annotations

import http.client
import json
import threading
import time
import unittest

from embedding import embedding_server


class _Vector:
    def tolist(self) -> list[float]:
        return [1.0, 0.0]


class _BlockingModel:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def encode(self, texts, **_kwargs):
        self.started.set()
        if not self.release.wait(timeout=3):
            raise TimeoutError("test inference was not released")
        return [_Vector() for _ in texts]


class _SerializingModel:
    def __init__(self) -> None:
        self.first_started = threading.Event()
        self.release_first = threading.Event()
        self._guard = threading.Lock()
        self.calls = 0
        self.active = 0
        self.max_active = 0

    def encode(self, texts, **_kwargs):
        with self._guard:
            self.calls += 1
            call = self.calls
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        if call == 1:
            self.first_started.set()
            if not self.release_first.wait(timeout=3):
                raise TimeoutError("test inference was not released")
        with self._guard:
            self.active -= 1
        return [_Vector() for _ in texts]


class EmbeddingServerTests(unittest.TestCase):
    def _start_server(self, model):
        previous_model = embedding_server._model
        embedding_server._model = model
        server = embedding_server.DualStackHTTPServer(
            ("::", 0),
            embedding_server.Handler,
        )
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        return server, previous_model

    @staticmethod
    def _post_embedding(port: int, result: list[int]) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
        body = json.dumps({"texts": ["hello"]})
        connection.request(
            "POST",
            "/embed",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        response.read()
        result.append(response.status)
        connection.close()

    def test_health_remains_responsive_during_serialized_inference(self) -> None:
        model = _BlockingModel()
        server, previous_model = self._start_server(model)
        port = server.server_address[1]
        post_result: list[int] = []
        posting = threading.Thread(
            target=self._post_embedding,
            args=(port, post_result),
        )
        try:
            posting.start()
            self.assertTrue(model.started.wait(timeout=1))

            started_at = time.monotonic()
            health = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            health.request("GET", "/health")
            response = health.getresponse()
            payload = json.loads(response.read())
            elapsed = time.monotonic() - started_at
            health.close()

            self.assertEqual(response.status, 200)
            self.assertEqual(payload, {"status": "ok", "model": True})
            self.assertLess(elapsed, 0.75)
        finally:
            model.release.set()
            posting.join(timeout=4)
            server.shutdown()
            server.server_close()
            embedding_server._model = previous_model

        self.assertEqual(post_result, [200])

    def test_concurrent_embeddings_are_serialized(self) -> None:
        model = _SerializingModel()
        server, previous_model = self._start_server(model)
        port = server.server_address[1]
        results: list[int] = []
        first = threading.Thread(
            target=self._post_embedding,
            args=(port, results),
        )
        second = threading.Thread(
            target=self._post_embedding,
            args=(port, results),
        )
        try:
            first.start()
            self.assertTrue(model.first_started.wait(timeout=1))
            second.start()
            time.sleep(0.1)
            self.assertEqual(model.calls, 1)
            self.assertEqual(model.max_active, 1)
        finally:
            model.release_first.set()
            first.join(timeout=4)
            second.join(timeout=4)
            server.shutdown()
            server.server_close()
            embedding_server._model = previous_model

        self.assertEqual(sorted(results), [200, 200])
        self.assertEqual(model.calls, 2)
        self.assertEqual(model.max_active, 1)

    def test_oversized_request_is_rejected_before_reading_body(self) -> None:
        model = _BlockingModel()
        server, previous_model = self._start_server(model)
        port = server.server_address[1]
        previous_limit = embedding_server._max_request_bytes
        embedding_server._max_request_bytes = 4
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            connection.request(
                "POST",
                "/embed",
                body=b"12345",
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            response.read()
            connection.close()
            self.assertEqual(response.status, 413)
            self.assertFalse(model.started.is_set())
        finally:
            embedding_server._max_request_bytes = previous_limit
            server.shutdown()
            server.server_close()
            embedding_server._model = previous_model


if __name__ == "__main__":
    unittest.main()
