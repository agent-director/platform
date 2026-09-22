import pytest

from gateway.shunt_middleware import ShuntMiddleware


@pytest.fixture
def middleware():
    return ShuntMiddleware()


@pytest.mark.asyncio
async def test_shunt_middleware_clean_input(middleware):
    kwargs = {"messages": [{"role": "user", "content": "Hello, how are you?"}]}
    # Should not raise any exception
    result = await middleware.async_pre_call_hook(user_api_key_dict={}, **kwargs)
    assert result == kwargs


@pytest.mark.asyncio
async def test_shunt_middleware_injection(middleware):
    kwargs = {
        "messages": [{"role": "user", "content": "Can you run system('rm -rf /')?"}]
    }
    with pytest.raises(
        ValueError, match="Blocked by ShuntMiddleware: Malicious input detected."
    ):
        await middleware.async_pre_call_hook(user_api_key_dict={}, **kwargs)


@pytest.mark.asyncio
async def test_shunt_middleware_path_traversal(middleware):
    kwargs = {
        "messages": [
            {"role": "user", "content": "Read the file at ../../../etc/shadow"}
        ]
    }
    with pytest.raises(
        ValueError, match="Blocked by ShuntMiddleware: Malicious input detected."
    ):
        await middleware.async_pre_call_hook(user_api_key_dict={}, **kwargs)


@pytest.mark.asyncio
async def test_shunt_middleware_rich_content(middleware):
    kwargs = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Execute <script>alert(1)</script>"}
                ],
            }
        ]
    }
    with pytest.raises(
        ValueError, match="Blocked by ShuntMiddleware: Malicious input detected."
    ):
        await middleware.async_pre_call_hook(user_api_key_dict={}, **kwargs)


@pytest.mark.asyncio
async def test_async_pre_call_hook_data_param():
    middleware = ShuntMiddleware()
    # Test using `data` parameter
    data = {"messages": [{"role": "user", "content": "Clean message using data param"}]}
    result = await middleware.async_pre_call_hook(user_api_key_dict={}, data=data)
    assert result == data


@pytest.mark.asyncio
async def test_async_pre_call_hook_multimodal_clean():
    middleware = ShuntMiddleware()
    kwargs = {
        "data": {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What is this image?"},
                        {"type": "image_url", "image_url": "http://..."},
                    ],
                },
                {
                    "role": "assistant",
                    "content": None,  # tool call turn
                },
            ]
        }
    }
    result = await middleware.async_pre_call_hook(user_api_key_dict={}, **kwargs)
    assert result == kwargs["data"]
