import json
from gateway.shunt_middleware import truncate_context, apply_shunt_middleware


def test_truncate_context_pins_system_messages():
    messages = [
        {"role": "system", "content": "A" * 4000},  # 1000 tokens
        {"role": "user", "content": "B" * 12000},  # 3000 tokens
        {"role": "system", "content": "C" * 4000},  # 1000 tokens
    ]
    # Total system tokens = 2000. Limit is 6000 to retain the 3000 token user message.
    result = truncate_context(messages, max_tokens=6000)
    # Both system messages must be retained, user message is retained
    assert len(result) == 3
    assert result[0]["role"] == "system"
    assert result[2]["role"] == "system"


def test_truncate_context_truncates_old_messages():
    messages = [
        {"role": "system", "content": "A" * 4000},  # 1000 tokens
        {"role": "user", "content": "B" * 8000},  # 2000 tokens
        {"role": "assistant", "content": "C" * 8000},  # 2000 tokens
        {"role": "user", "content": "D" * 4000},  # 1000 tokens
    ]
    # Total = 6000 tokens. System = 1000 tokens. Max = 4000.
    # Allowed for others = 3000.
    # From end: D (1000) -> OK.
    # C (2000) -> OK. 1000+2000 = 3000.
    # B (2000) -> Over capacity, discarded.
    result = truncate_context(messages, max_tokens=4000)
    assert len(result) == 3
    assert result[0]["role"] == "system"
    assert result[1]["content"] == "C" * 8000
    assert result[2]["content"] == "D" * 4000


def test_truncate_context_system_exceeds_limit():
    messages = [
        {"role": "system", "content": "A" * 20000},  # 5000 tokens
        {"role": "user", "content": "B" * 4000},  # 1000 tokens
    ]
    # System exceeds 4000 limit.
    # Should return only system messages.
    result = truncate_context(messages, max_tokens=4000)
    assert len(result) == 1
    assert result[0]["role"] == "system"
    assert result[0]["content"] == "A" * 20000


def test_apply_shunt_middleware():
    payload_dict = {
        "model": "gpt-4",
        "messages": [
            {"role": "system", "content": "A" * 100},
            {
                "role": "user",
                "content": "B" * 40000,
            },  # 10000 tokens, over default 8000 limit
        ],
    }
    payload_bytes = json.dumps(payload_dict).encode("utf-8")

    result_bytes = apply_shunt_middleware(payload_bytes)
    result_dict = json.loads(result_bytes.decode("utf-8"))

    assert "messages" in result_dict
    assert len(result_dict["messages"]) == 1
    assert result_dict["messages"][0]["role"] == "system"


def test_apply_shunt_middleware_invalid_json():
    payload_bytes = b"not valid json"
    result_bytes = apply_shunt_middleware(payload_bytes)
    assert result_bytes == b"not valid json"
