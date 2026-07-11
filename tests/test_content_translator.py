import json

import pytest

from core.content_translator import resolve_translation_api_key, translate_aweme_content


@pytest.mark.asyncio
async def test_translate_aweme_content_uses_env_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")

    captured = {}

    class _FakeResponse:
        status = 200

        async def text(self):
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "title_vi": "Tiêu đề",
                                        "description_vi": "Mô tả video",
                                        "tags_vi": ["robot", "đồ chơi"],
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                }
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return _FakeResponse()

    monkeypatch.setattr("core.content_translator.aiohttp.ClientSession", lambda **kwargs: _FakeSession())

    result = await translate_aweme_content(
        "#标签 中文标题",
        ["标签"],
        {"enabled": True, "api_key_env": "OPENAI_API_KEY"},
    )

    assert result is not None
    assert result["title_vi"] == "Tiêu đề"
    assert result["description_vi"] == "Mô tả video"
    assert result["tags_vi"] == ["robot", "đồ chơi"]
    assert captured["headers"]["Authorization"] == "Bearer sk-test-key"


def test_resolve_translation_api_key_from_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    key, source = resolve_translation_api_key({"api_key_env": "OPENAI_API_KEY"})
    assert key == "sk-env"
    assert source == "env"
