"""
Tests for VLM (Vision Language Model) via POST /v1/chat/completions.

Covers: base64 image, image_url, multi-image, and VLM model routing.
"""

from __future__ import annotations

import base64


from tests.conftest import auth_header, make_httpx_response


def _make_post_coro(response):
    async def _post(*args, **kwargs):
        return response
    return _post


# A tiny 1x1 red PNG for testing
_TINY_PNG = base64.b64encode(
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00"
    b"\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00"
    b"\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
).decode()


class TestVLMWithImage:

    def test_base64_image(self, client, test_user):
        """Send a base64-encoded image in the messages."""
        downstream_body = {
            "id": "chatcmpl-vlm1",
            "object": "chat.completion",
            "model": "real-vlm-v1",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "I see a red pixel."},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
        }
        mock = client.__httpx_mock__
        mock.post = _make_post_coro(make_httpx_response(200, downstream_body))

        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-vlm",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "What do you see in this image?"},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{_TINY_PNG}",
                                },
                            },
                        ],
                    }
                ],
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        data = resp.json()
        assert "red" in data["choices"][0]["message"]["content"].lower()

    def test_image_url_remote(self, client, test_user):
        """Send a remote image URL in the messages."""
        downstream_body = {
            "id": "chatcmpl-vlm2",
            "object": "chat.completion",
            "model": "real-vlm-v1",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "This is a photo of a cat."},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 200, "completion_tokens": 12, "total_tokens": 212},
        }
        mock = client.__httpx_mock__
        mock.post = _make_post_coro(make_httpx_response(200, downstream_body))

        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-vlm",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Describe this image."},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": "https://example.com/cat.jpg",
                                },
                            },
                        ],
                    }
                ],
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["choices"][0]["message"]["content"] == "This is a photo of a cat."

    def test_multiple_images(self, client, test_user):
        """Send multiple images in a single message."""
        downstream_body = {
            "id": "chatcmpl-vlm3",
            "object": "chat.completion",
            "model": "real-vlm-v1",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Image 1 shows a red pixel. Image 2 is a cat."},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 300, "completion_tokens": 20, "total_tokens": 320},
        }
        mock = client.__httpx_mock__
        mock.post = _make_post_coro(make_httpx_response(200, downstream_body))

        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-vlm",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Compare these two images."},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{_TINY_PNG}"},
                            },
                            {
                                "type": "image_url",
                                "image_url": {"url": "https://example.com/cat.jpg"},
                            },
                        ],
                    }
                ],
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        data = resp.json()
        assert "Image 1" in data["choices"][0]["message"]["content"]

    def test_vlm_model_routing(self, client, test_user):
        """VLM model should be routed correctly (type=vlm)."""
        downstream_body = {
            "id": "chatcmpl-vlm-route",
            "object": "chat.completion",
            "model": "real-vlm-v1",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "routed correctly"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
        }

        captured_args = {}

        async def capture_post(*args, **kwargs):
            captured_args["url"] = args[0] if args else kwargs.get("url")
            captured_args["json"] = kwargs.get("json", {})
            return make_httpx_response(200, downstream_body)

        mock = client.__httpx_mock__
        mock.post = capture_post

        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-vlm",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Describe this."},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{_TINY_PNG}"},
                            },
                        ],
                    }
                ],
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        # Verify the model was swapped to real_model
        assert captured_args.get("json", {}).get("model") == "real-vlm-v1"
        # Verify routed to VLM base_url
        assert "mock-vlm:8001" in str(captured_args.get("url", ""))

    def test_image_with_detail_parameter(self, client, test_user):
        """OpenAI supports a 'detail' parameter on image_url."""
        downstream_body = {
            "id": "chatcmpl-vlm-detail",
            "object": "chat.completion",
            "model": "real-vlm-v1",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Detailed analysis."},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 500, "completion_tokens": 8, "total_tokens": 508},
        }
        mock = client.__httpx_mock__
        mock.post = _make_post_coro(make_httpx_response(200, downstream_body))

        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-vlm",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Analyze in detail."},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{_TINY_PNG}",
                                    "detail": "high",
                                },
                            },
                        ],
                    }
                ],
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
