import json
import os
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from utils.logger import setup_logger

logger = setup_logger("ContentTranslator")

DEFAULT_API_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_API_KEY_ENV = "OPENAI_API_KEY"
DEFAULT_HTML_PROXY_URL = "http://127.0.0.1:8095/v1/responses"
DEFAULT_HTML_PROXY_TIMEOUT_SECONDS = 600
HTML_PROXY_PROFILE_HEADER = "X-ChatGPT-Profile"
DEFAULT_HTML_PROXY_PROFILE = "title"

SYSTEM_PROMPT = (
    "You translate Douyin video metadata from Chinese to natural Vietnamese for "
    "Facebook/YouTube uploads. Return strict JSON only with keys: "
    "title_vi (short catchy title, no hashtags), "
    "description_vi (full Vietnamese caption/body, no hashtags), "
    "tags_vi (array of Vietnamese hashtag strings without # prefix). "
    "Keep brand names and proper nouns when appropriate."
)

HTML_PROXY_SYSTEM_PROMPT = (
    "You prepare Vietnamese Facebook/YouTube metadata from a Chinese Douyin video.\n"
    "Return VALID JSON only. No Markdown, no code fences, no explanation.\n"
    "\n"
    "Output object keys (exact):\n"
    "{\n"
    '  "title_vi": "string",\n'
    '  "description_vi": "string",\n'
    '  "tags_vi": ["string", "..."]\n'
    "}\n"
    "\n"
    "Rules:\n"
    "- title_vi: short catchy natural Vietnamese title; no hashtags; not literal translation.\n"
    "- description_vi: natural Vietnamese caption/body; no hashtags.\n"
    "- tags_vi: SUGGEST 5-8 Vietnamese discovery hashtags based on the video topic "
    "(strings WITHOUT #, no spaces inside each tag). "
    "Do NOT translate Chinese source_tags one-by-one. "
    "source_tags are optional context only — invent relevant Vietnamese tags.\n"
    "- No Chinese characters in title_vi, description_vi, or tags_vi.\n"
    "- Do not invent facts not implied by the source context."
)


def resolve_translation_api_key(translation_cfg: Dict[str, Any]) -> Tuple[str, str]:
    settings_value = str(translation_cfg.get("api_key", "") or "").strip()
    if settings_value:
        return settings_value, "settings"

    api_key_env = str(translation_cfg.get("api_key_env", DEFAULT_API_KEY_ENV) or "").strip()
    if api_key_env:
        env_value = os.getenv(api_key_env, "").strip()
        if env_value:
            return env_value, "env"
    return "", "none"


def _is_enabled(translation_cfg: Dict[str, Any]) -> bool:
    if not isinstance(translation_cfg, dict):
        return False
    enabled = translation_cfg.get("enabled")
    if isinstance(enabled, str):
        return enabled.strip().lower() in {"1", "true", "yes", "on"}
    return bool(enabled)


def _is_truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def use_chatgpt_html_proxy(translation_cfg: Dict[str, Any]) -> bool:
    return _is_truthy(translation_cfg.get("use_chatgpt_html_proxy"))


def build_fallback_vi_content(desc: str, tags: Optional[List[str]] = None) -> Dict[str, Any]:
    return {
        "title_vi": "",
        "description_vi": "",
        "tags_vi": [],
    }


def _normalize_tags(raw_tags: Any) -> List[str]:
    if not isinstance(raw_tags, list):
        return []
    tags: List[str] = []
    for tag in raw_tags:
        text = str(tag).strip().lstrip("#")
        if text:
            tags.append(text)
    return tags


def _parse_vi_payload(content: str) -> Optional[Dict[str, Any]]:
    parsed = json.loads(content)
    if isinstance(parsed, str):
        parsed = json.loads(parsed)
    if not isinstance(parsed, dict):
        return None
    title_vi = str(parsed.get("title_vi") or "").strip()
    description_vi = str(parsed.get("description_vi") or title_vi or "").strip()
    raw_tags = parsed.get("tags_vi")
    if raw_tags is None:
        raw_tags = parsed.get("hashtags")
    tags_vi = _normalize_tags(raw_tags)
    if not title_vi and not description_vi and not tags_vi:
        return None
    return {
        "title_vi": title_vi or description_vi,
        "description_vi": description_vi or title_vi,
        "tags_vi": tags_vi,
    }


def extract_responses_output_text(data: Dict[str, Any]) -> str:
    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    output = data.get("output") or []
    if isinstance(output, list) and output:
        content = (output[0] or {}).get("content") or []
        if isinstance(content, list) and content:
            text = (content[0] or {}).get("text")
            if isinstance(text, str):
                return text.strip()
    return ""


def build_html_proxy_input(desc: str, tags: List[str]) -> str:
    context = {
        "desc": desc or "",
        "source_tags": tags or [],
    }
    return (
        "Source context JSON:\n"
        f"{json.dumps(context, ensure_ascii=False, indent=2)}\n\n"
        "Generate title_vi, description_vi, and SUGGEST tags_vi (Vietnamese hashtags) "
        "as described in your instructions. Return JSON only."
    )


async def _translate_via_openai_chat(
    desc: str,
    tags: List[str],
    translation_cfg: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    api_key, source = resolve_translation_api_key(translation_cfg)
    if not api_key:
        logger.warning(
            "Translation enabled but no API key found (env %s)",
            translation_cfg.get("api_key_env", DEFAULT_API_KEY_ENV),
        )
        return None

    model = str(translation_cfg.get("model", DEFAULT_MODEL) or DEFAULT_MODEL)
    api_url = str(translation_cfg.get("api_url", DEFAULT_API_URL) or DEFAULT_API_URL)
    payload = {"desc": desc or "", "tags": tags or []}
    request_body = {
        "model": model,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(api_url, headers=headers, json=request_body) as resp:
            body = await resp.text()
            if resp.status >= 400:
                logger.warning(
                    "Translation API failed (status=%s, source=%s): %s",
                    resp.status,
                    source,
                    body[:300],
                )
                return None
            data = json.loads(body)

    content = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
    return _parse_vi_payload(content)


async def _translate_via_html_proxy(
    desc: str,
    tags: List[str],
    translation_cfg: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    model = str(translation_cfg.get("model", DEFAULT_MODEL) or DEFAULT_MODEL)
    api_url = str(
        translation_cfg.get("html_proxy_url")
        or translation_cfg.get("chatgpt_html_proxy_url")
        or DEFAULT_HTML_PROXY_URL
    ).strip()
    timeout_raw = translation_cfg.get(
        "html_proxy_timeout_seconds", DEFAULT_HTML_PROXY_TIMEOUT_SECONDS
    )
    try:
        timeout_seconds = max(30, int(timeout_raw))
    except (TypeError, ValueError):
        timeout_seconds = DEFAULT_HTML_PROXY_TIMEOUT_SECONDS

    request_body = {
        "model": model,
        "temperature": 0.4,
        "instructions": HTML_PROXY_SYSTEM_PROMPT,
        "input": build_html_proxy_input(desc, tags),
    }
    headers = {"Content-Type": "application/json"}
    profile = str(
        translation_cfg.get("html_proxy_profile")
        or translation_cfg.get("chatgpt_html_proxy_profile")
        or DEFAULT_HTML_PROXY_PROFILE
    ).strip() or DEFAULT_HTML_PROXY_PROFILE
    headers[HTML_PROXY_PROFILE_HEADER] = profile
    api_key, _ = resolve_translation_api_key(translation_cfg)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    logger.info(
        "Metadata via chatgpt-html-proxy (title/desc + suggest hashtags) url=%s profile=%s model=%s timeout=%ss",
        api_url,
        profile,
        model,
        timeout_seconds,
    )

    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(api_url, headers=headers, json=request_body) as resp:
            body = await resp.text()
            if resp.status >= 400:
                logger.warning(
                    "chatgpt-html-proxy metadata failed (status=%s): %s",
                    resp.status,
                    body[:300],
                )
                return None
            data = json.loads(body)

    content = extract_responses_output_text(data if isinstance(data, dict) else {})
    if not content:
        logger.warning("chatgpt-html-proxy returned empty output text")
        return None
    return _parse_vi_payload(content)


async def translate_aweme_content(
    desc: str,
    tags: List[str],
    translation_cfg: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    if not _is_enabled(translation_cfg):
        return None

    try:
        if use_chatgpt_html_proxy(translation_cfg):
            return await _translate_via_html_proxy(desc, tags, translation_cfg)
        return await _translate_via_openai_chat(desc, tags, translation_cfg)
    except Exception as exc:
        logger.warning("Translation failed: %s", exc)
        return None


async def resolve_aweme_vi_content(
    desc: str,
    tags: List[str],
    translation_cfg: Dict[str, Any],
) -> Tuple[Dict[str, Any], bool]:
    translated = await translate_aweme_content(desc, tags, translation_cfg)
    if translated:
        return translated, True

    empty = build_fallback_vi_content(desc, tags)
    if _is_enabled(translation_cfg):
        backend = (
            "chatgpt-html-proxy"
            if use_chatgpt_html_proxy(translation_cfg)
            else "OpenAI chat/completions"
        )
        logger.warning(
            "Leaving title_vi/description_vi/tags_vi empty because %s translation "
            "was unavailable (will not copy Chinese source text)",
            backend,
        )
    return empty, False
