from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

from backend.config.settings import Settings, get_settings

log = logging.getLogger("slothpay.intelligence")

T = TypeVar("T", bound=BaseModel)
MAX_RESPONSE_BYTES = 256 * 1024
_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)
_MAX_START_POSITIONS = 20


def parse_json_object(text: object) -> dict[str, Any] | None:
    """The first JSON object in a model answer, tolerating code fences and chatter."""
    if not isinstance(text, str):
        return None
    fenced = _FENCE.search(text)
    candidate = fenced.group(1) if fenced else text
    decoder = json.JSONDecoder()
    position = -1
    for _ in range(_MAX_START_POSITIONS):
        position = candidate.find("{", position + 1)
        if position < 0:
            return None
        try:
            value, _end = decoder.raw_decode(candidate, position)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _message_text(payload: Any) -> str | None:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    if isinstance(content, list):  # some servers return a list of text parts
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content if isinstance(content, str) else None


class ModelClient:
    def __init__(self, settings: Settings | None = None, http: httpx.AsyncClient | None = None):
        settings = settings or get_settings()
        base = (settings.GEMMA_BASE_URL or "").strip().rstrip("/")
        self._base = base if base.lower().startswith(("http://", "https://")) else ""
        self._key = settings.GEMMA_API_KEY or ""
        self._model = settings.GEMMA_MODEL or ""
        self._http = http

    @property
    def enabled(self) -> bool:
        """False when no endpoint is configured: every call then returns None at once."""
        return bool(self._base)

    async def complete_json(
        self, system: str, user: str, schema: type[T], timeout: float = 8.0
    ) -> T | None:
        if not self.enabled:
            return None
        body: dict[str, Any] = {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": 800,
            "stream": False,
        }
        if self._model:
            body["model"] = self._model
        headers = {"Content-Type": "application/json"}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        try:
            async with asyncio.timeout(timeout):
                response = await self._post(
                    f"{self._base}/chat/completions", body, headers, timeout
                )
            if response.status_code != 200 or len(response.content) > MAX_RESPONSE_BYTES:
                log.info("model call refused: HTTP %s", response.status_code)
                return None
            data = parse_json_object(_message_text(response.json()))
            return schema.model_validate(data) if data is not None else None
        except Exception as exc:  # timeout, network, bad JSON, wrong shape: all mean "no answer"
            log.info("model call failed: %s", type(exc).__name__)
            return None

    async def _post(
        self, url: str, body: dict[str, Any], headers: dict[str, str], timeout: float
    ) -> httpx.Response:
        if self._http is not None:
            return await self._http.post(url, json=body, headers=headers, timeout=timeout)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as http:
            return await http.post(url, json=body, headers=headers)