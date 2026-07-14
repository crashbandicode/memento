import builtins
import sys
from types import SimpleNamespace
from unittest.mock import patch

from server.services.tokenize import tokenize_for_index


def test_ascii_indexing_does_not_import_jieba():
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "jieba":
            raise AssertionError("ASCII text must not import jieba")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=guarded_import):
        assert tokenize_for_index("Docker/PVC hello-world") == (
            "docker pvc hello world"
        )


def test_mixed_text_only_sends_cjk_spans_to_jieba():
    seen = []

    def cut_for_search(piece):
        seen.append(piece)
        return ("中文", "测试")

    fake_jieba = SimpleNamespace(cut_for_search=cut_for_search)
    with patch.dict(sys.modules, {"jieba": fake_jieba}):
        assert tokenize_for_index("Docker 中文测试 PVC") == (
            "docker 中文 测试 pvc"
        )

    assert seen == ["中文测试"]
