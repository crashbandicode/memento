from __future__ import annotations

import pytest

from server.scripts import null_document_content


@pytest.mark.asyncio
async def test_null_document_content_is_a_clear_successful_no_op() -> None:
    result = await null_document_content.run(apply=True, batch_size=10)

    assert result["status"] == "no-op"
    assert "dropped" in str(result["message"])
