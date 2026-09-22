import re
from typing import Any

from litellm.integrations.custom_logger import CustomLogger


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
        """
        Intercept the LLM call before it goes out.
        We can check for prompt injection or validate token limits.
        """
        data_dict = data if data is not None else kwargs.copy()
        messages = data_dict.get("messages", [])
        for msg in messages:
            content = msg.get("content")
            if content is None:
                # Skip empty content blocks or tool call turns without content
                continue
            elif isinstance(content, str):
                if not self._is_safe_input(content):
                    raise ValueError(
                        "Blocked by ShuntMiddleware: Malicious input detected."
                    )
            elif isinstance(content, list):
                # handle multi-modal or rich content
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
        """
        Telemetry and tracking.
        """
