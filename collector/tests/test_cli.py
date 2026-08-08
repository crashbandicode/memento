import io
import os
from pathlib import Path

from collector.cli import RECENT_LOG_READ_BYTES, _read_recent_log_line


class _TrackingReader(io.BytesIO):
    def __init__(self, value: bytes) -> None:
        super().__init__(value)
        self.read_sizes: list[int] = []
        self.seek_calls: list[tuple[int, int]] = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return super().read(size)

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        self.seek_calls.append((offset, whence))
        return super().seek(offset, whence)


def test_recent_log_tail_reads_only_bounded_suffix(monkeypatch, tmp_path):
    payload = b"x" * (RECENT_LOG_READ_BYTES * 2) + b"\nlast useful line\n"
    reader = _TrackingReader(payload)
    log_file = tmp_path / "collector.log"

    def fake_open(path: Path, mode: str):
        assert path == log_file
        assert mode == "rb"
        return reader

    monkeypatch.setattr(Path, "open", fake_open)

    assert _read_recent_log_line(log_file) == "last useful line"
    assert reader.read_sizes == [RECENT_LOG_READ_BYTES]
    assert reader.seek_calls == [
        (0, os.SEEK_END),
        (len(payload) - RECENT_LOG_READ_BYTES, os.SEEK_SET),
    ]


def test_recent_log_tail_falls_back_to_rotation(tmp_path):
    log_file = tmp_path / "collector.log"
    log_file.write_bytes(b"")
    Path(f"{log_file}.1").write_text("rotated last line\n", encoding="utf-8")

    assert _read_recent_log_line(log_file) == "rotated last line"
