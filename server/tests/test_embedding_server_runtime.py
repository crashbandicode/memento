"""Pure unit tests for embedding server runtime/device/provider config.

These tests never download a model. Device availability is injected.
"""

from __future__ import annotations

import math
import os
import unittest
from unittest.mock import patch

from server.services import embedding_server as es


class ResolveBackendTests(unittest.TestCase):
    def test_accepts_known_values(self) -> None:
        cases = [
            (None, "torch"),
            ("", "torch"),
            ("TORCH", "torch"),
            ("onnx", "onnx"),
            ("Onnx", "onnx"),
        ]
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MEMENTO_EMBEDDING_BACKEND", None)
            for raw, expected in cases:
                with self.subTest(raw=raw):
                    self.assertEqual(es.resolve_backend(raw), expected)

    def test_rejects_unknown(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MEMENTO_EMBEDDING_BACKEND", None)
            with self.assertRaisesRegex(ValueError, "MEMENTO_EMBEDDING_BACKEND"):
                es.resolve_backend("openvino")


class ResolveDeviceTests(unittest.TestCase):
    def test_policy_matrix(self) -> None:
        cases = [
            ("torch", "auto", True, True, "Linux", "cuda"),
            ("torch", "auto", False, True, "Darwin", "cpu"),
            ("torch", "auto", False, False, "Linux", "cpu"),
            ("torch", "cpu", True, True, "Linux", "cpu"),
            ("torch", "cuda", True, False, "Linux", "cuda"),
            ("torch", "mps", False, True, "Darwin", "mps"),
            ("onnx", "auto", True, True, "Linux", "cpu"),
            ("onnx", "cpu", True, False, "Linux", "cpu"),
        ]
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MEMENTO_EMBEDDING_DEVICE", None)
            for backend, requested, cuda, mps, system, expected in cases:
                with self.subTest(backend=backend, requested=requested, system=system):
                    self.assertEqual(
                        es.resolve_device(
                            requested,
                            backend=backend,
                            cuda_usable=cuda,
                            mps_usable=mps,
                            system=system,
                        ),
                        expected,
                    )

    def test_rejects_unsupported(self) -> None:
        cases = [
            ("torch", "cuda", False, False, "Linux", "CUDA is not available"),
            ("torch", "mps", False, False, "Darwin", "MPS is not available"),
            ("torch", "mps", False, True, "Linux", "only supported on macOS"),
            ("onnx", "cuda", True, False, "Linux", "does not support"),
            ("onnx", "mps", False, True, "Darwin", "does not support"),
            ("torch", "tpu", False, False, "Linux", "Invalid MEMENTO_EMBEDDING_DEVICE"),
        ]
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MEMENTO_EMBEDDING_DEVICE", None)
            for backend, requested, cuda, mps, system, match in cases:
                with self.subTest(backend=backend, requested=requested):
                    with self.assertRaisesRegex(ValueError, match):
                        es.resolve_device(
                            requested,
                            backend=backend,
                            cuda_usable=cuda,
                            mps_usable=mps,
                            system=system,
                        )


class ResolveProviderAndHealthTests(unittest.TestCase):
    def test_onnx_provider_defaults(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MEMENTO_EMBEDDING_ONNX_PROVIDER", None)
            self.assertEqual(
                es.resolve_onnx_provider(
                    None,
                    backend="onnx",
                    available_providers=["CPUExecutionProvider"],
                ),
                "CPUExecutionProvider",
            )
            self.assertIsNone(es.resolve_onnx_provider(None, backend="torch"))
            self.assertEqual(
                es.resolve_onnx_provider(
                    "CUDAExecutionProvider",
                    backend="onnx",
                    available_providers=[
                        "CPUExecutionProvider",
                        "CUDAExecutionProvider",
                    ],
                ),
                "CUDAExecutionProvider",
            )
            with self.assertRaisesRegex(ValueError, "unavailable"):
                es.resolve_onnx_provider(
                    "CUDAExecutionProvider",
                    backend="onnx",
                    available_providers=["CPUExecutionProvider"],
                )

    def test_build_runtime_info_onnx_health_metadata(self) -> None:
        env = {
            "MEMENTO_EMBEDDING_BACKEND": "onnx",
            "MEMENTO_EMBEDDING_DEVICE": "auto",
            "MEMENTO_EMBEDDING_ONNX_FILE": "model_qint8_avx2.onnx",
        }
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("MEMENTO_EMBEDDING_ONNX_PROVIDER", None)
            info = es.build_runtime_info(
                "BAAI/bge-m3",
                dimension=1024,
                cuda_usable=True,
                mps_usable=True,
                system="Darwin",
                available_onnx_providers=["CPUExecutionProvider"],
            )
            self.assertEqual(info.backend, "onnx")
            self.assertEqual(info.device, "cpu")
            self.assertEqual(info.provider, "CPUExecutionProvider")
            self.assertEqual(info.onnx_file, "model_qint8_avx2.onnx")
            self.assertEqual(info.dimension, 1024)
            payload = info.health_payload(model_loaded=True)
            self.assertEqual(payload["status"], "ok")
            self.assertTrue(payload["model"])
            self.assertEqual(payload["model_name"], "BAAI/bge-m3")
            self.assertEqual(payload["backend"], "onnx")
            self.assertEqual(payload["device"], "cpu")
            self.assertEqual(payload["provider"], "CPUExecutionProvider")
            self.assertEqual(payload["providers"], [])
            self.assertEqual(payload["dimension"], 1024)
            self.assertEqual(payload["onnx_file"], "model_qint8_avx2.onnx")
            self.assertTrue(payload["profile_signature"].startswith("sha256:"))
            self.assertEqual(payload["cpu_threads"], 1)

    def test_build_runtime_info_torch_auto_prefers_cuda(self) -> None:
        env = {
            "MEMENTO_EMBEDDING_BACKEND": "torch",
            "MEMENTO_EMBEDDING_DEVICE": "auto",
        }
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("MEMENTO_EMBEDDING_ONNX_FILE", None)
            os.environ.pop("MEMENTO_EMBEDDING_ONNX_PROVIDER", None)
            info = es.build_runtime_info(
                "BAAI/bge-m3",
                dimension=1024,
                cuda_usable=True,
                mps_usable=True,
                system="Linux",
            )
            self.assertEqual(info.backend, "torch")
            self.assertEqual(info.device, "cuda")
            self.assertIsNone(info.provider)
            self.assertIsNone(info.onnx_file)
            payload = info.health_payload(model_loaded=True)
            self.assertEqual(payload["backend"], "torch")
            self.assertEqual(payload["device"], "cuda")
            self.assertEqual(payload["dimension"], 1024)
            self.assertTrue(payload["model"])

    def test_cpu_threads_are_bounded(self) -> None:
        self.assertEqual(es.resolve_cpu_threads("1"), 1)
        self.assertEqual(es.resolve_cpu_threads("64"), 64)
        for value in ("0", "65", "many"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "CPU_THREADS"):
                    es.resolve_cpu_threads(value)

    def test_profile_signature_changes_with_vector_space_policy(self) -> None:
        base = {
            "model_name": "BAAI/bge-m3",
            "model_revision": "revision-a",
            "backend": "onnx",
            "dimension": 1024,
            "query_prefix": "",
            "document_prefix": "",
            "max_sequence_length": 8192,
            "onnx_file": "onnx/model.onnx",
            "artifact_sha256": "a" * 64,
        }
        signature = es.embedding_profile_signature(**base)
        for field, value in (
            ("model_revision", "revision-b"),
            ("backend", "torch"),
            ("query_prefix", "query: "),
            ("artifact_sha256", "b" * 64),
        ):
            with self.subTest(field=field):
                changed = {**base, field: value}
                self.assertNotEqual(
                    signature,
                    es.embedding_profile_signature(**changed),
                )

    def test_purpose_prefixes_are_asymmetric(self) -> None:
        runtime = es.RuntimeInfo(
            model_name="intfloat/multilingual-e5-small",
            backend="torch",
            device="cpu",
            provider=None,
            dimension=384,
            query_prefix="query: ",
            document_prefix="passage: ",
        )
        self.assertEqual(
            es._prepare_texts(["hello"], purpose="query", runtime=runtime),
            ["query: hello"],
        )
        self.assertEqual(
            es._prepare_texts(["hello"], purpose="document", runtime=runtime),
            ["passage: hello"],
        )

    def test_output_probe_rejects_bad_vectors(self) -> None:
        self.assertEqual(
            es.validate_embedding_vectors(
                [[1.0, 0.0]],
                expected_count=1,
                dimension=2,
            ),
            [[1.0, 0.0]],
        )
        for vector, expected in (
            ([math.nan, 0.0], "non-finite"),
            ([0.5, 0.0], "not normalized"),
            ([1.0], "dimension mismatch"),
        ):
            with self.subTest(vector=vector):
                with self.assertRaisesRegex(ValueError, expected):
                    es.validate_embedding_vectors(
                        [vector],
                        expected_count=1,
                        dimension=2,
                    )


class RequestRuntimeValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = es.RuntimeInfo(
            model_name="BAAI/bge-m3",
            backend="onnx",
            device="cpu",
            provider="CPUExecutionProvider",
            dimension=1024,
            onnx_file="onnx/model_qint8_avx2.onnx",
        )

    def test_matching_or_legacy_request_is_accepted(self) -> None:
        self.assertIsNone(es.request_runtime_mismatch({}, self.runtime))
        self.assertIsNone(
            es.request_runtime_mismatch(
                {
                    "model": "BAAI/bge-m3",
                    "dimensions": 1024,
                    "backend": "onnx",
                },
                self.runtime,
            )
        )

    def test_wrong_profile_identity_is_rejected(self) -> None:
        cases = [
            ({"model": "another/model"}, "requested model"),
            ({"dimensions": 384}, "requested dimensions"),
            ({"backend": "torch"}, "requested backend"),
            ({"profile_signature": "sha256:wrong"}, "profile_signature"),
        ]
        for body, expected in cases:
            with self.subTest(body=body):
                self.assertIn(
                    expected,
                    es.request_runtime_mismatch(body, self.runtime) or "",
                )


if __name__ == "__main__":
    unittest.main()
