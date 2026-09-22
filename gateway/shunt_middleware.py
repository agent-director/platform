import json
import re
from typing import Any

from litellm.integrations.custom_logger import CustomLogger


def truncate_context(messages: list, max_tokens: int = 4000) -> list:
    """
    Intelligent Shunt Middleware: Deterministic Token Optimization.
    Ensures that system messages are pinned and never truncated.
    """
    if not messages:
        return messages

    system_messages = [m for m in messages if m.get("role") == "system"]
    other_messages = [m for m in messages if m.get("role") != "system"]

    def estimate_tokens(msg):
        if not isinstance(msg, dict):
            return 0
        content = msg.get("content", "")
        if isinstance(content, list):
            content_len = sum(
                len(str(part.get("text", "")))
                for part in content
                if isinstance(part, dict)
            )
        else:
            content_len = len(str(content))
        tool_calls = str(msg.get("tool_calls", ""))
        function_call = str(msg.get("function_call", ""))
        return (content_len + len(tool_calls) + len(function_call)) // 4

    sys_tokens = sum(estimate_tokens(m) for m in system_messages)

    if sys_tokens >= max_tokens:
        return system_messages

    allowed_tokens_for_others = max_tokens - sys_tokens
    retained_others = []
    current_other_tokens = 0

    for msg in reversed(other_messages):
        msg_tokens = estimate_tokens(msg)
        if current_other_tokens + msg_tokens <= allowed_tokens_for_others:
            retained_others.append(msg)
            current_other_tokens += msg_tokens
        else:
            break

    retained_others.reverse()
    retained_set = {id(m) for m in system_messages + retained_others}
    return [m for m in messages if id(m) in retained_set]


def apply_shunt_middleware(payload: bytes, user: str | None = None) -> bytes:
    try:
        data = json.loads(payload.decode("utf-8"))
        if "messages" in data and isinstance(data["messages"], list):
            data["messages"] = truncate_context(data["messages"], max_tokens=8000)
        if user:
            data["user"] = user
        return json.dumps(data).encode("utf-8")
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass  # Return raw payload if it's not JSON
    except Exception as e:
        import logging

        logging.getLogger(__name__).error(f"Shunt middleware error: {e}")
    return payload


class ShuntMiddleware(CustomLogger):
    def __init__(self):
        super().__init__()
        self.malicious_regex = re.compile(
            r"\.\./|<script>|system\(|exec\(", re.IGNORECASE
        )

    def _is_safe_input(self, text: str) -> bool:
        if not isinstance(text, str):
            return True
        return not bool(self.malicious_regex.search(text))

    async def async_pre_call_hook(
        self,
        user_api_key_dict: dict,
        cache: Any = None,
        data: dict | None = None,
        call_type: str | None = None,
        **kwargs,
    ):
        data_dict = data if data is not None else kwargs.copy()
        messages = data_dict.get("messages", [])
        for msg in messages:
            content = msg.get("content")
            if content is None:
                continue
            elif isinstance(content, str):
                if not self._is_safe_input(content):
                    raise ValueError(
                        "Blocked by ShuntMiddleware: Malicious input detected."
                    )
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        if not self._is_safe_input(part.get("text", "")):
                            raise ValueError(
                                "Blocked by ShuntMiddleware: Malicious input detected."
                            )
                    elif isinstance(part, str):
                        if not self._is_safe_input(part):
                            raise ValueError(
                                "Blocked by ShuntMiddleware: Malicious input detected."
                            )

        if data is not None:
            return data
        return data_dict

    async def async_post_call_success_hook(
        self, data: dict, user_api_key_dict: dict, response: Any, **kwargs
    ):
        pass
