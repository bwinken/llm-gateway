"""SigV4 signing for Bedrock IAM credentials (app/services/aws_sigv4.py)."""

from __future__ import annotations

from datetime import datetime, timezone

from app.services.aws_sigv4 import sign_request

_AK = "AKIDEXAMPLE"
_SK = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"


def _sig(headers: dict[str, str]) -> str:
    return headers["Authorization"].rsplit("Signature=", 1)[1]


class TestKnownVectors:
    def test_aws_test_suite_get_and_post_vanilla(self):
        """The `get-vanilla` / `post-vanilla` cases of AWS's published SigV4
        test suite."""
        now = datetime(2015, 8, 30, 12, 36, 0, tzinfo=timezone.utc)
        for method, expected in (
            ("GET", "5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"),
            ("POST", "5da7c1a2acd57cee7505fc6676e4e544621c30862966e37dddb68e92efbe5d6b"),
        ):
            headers = sign_request(
                method, "https://example.amazonaws.com/", b"",
                access_key=_AK, secret_key=_SK, region="us-east-1", service="service", now=now,
            )
            assert headers["X-Amz-Date"] == "20150830T123600Z"
            assert headers["Authorization"] == (
                "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
                f"SignedHeaders=host;x-amz-date, Signature={expected}"
            )

    def test_bedrock_model_id_with_colon_matches_botocore(self):
        """Pinned from botocore's SigV4Auth. The model ID's ':' is %3A in the
        URL and must be encoded again (%253A) in the canonical request."""
        headers = sign_request(
            "POST",
            "https://bedrock-runtime.us-east-1.amazonaws.com/model/"
            "anthropic.claude-sonnet-4-20250514-v1%3A0/converse",
            b'{"messages": [{"role": "user", "content": [{"text": "hi"}]}]}',
            access_key=_AK, secret_key=_SK, region="us-east-1", service="bedrock",
            now=datetime(2026, 10, 19, 8, 30, 0, tzinfo=timezone.utc),
        )
        assert _sig(headers) == "32d543a5cb2427b19e4edc37523b2460cd4707fb78520a38d18cd0bd51023539"


class TestSigningInputs:
    def _sign(self, url="https://bedrock-runtime.ap-northeast-1.amazonaws.com/model/m/converse",
              body=b"{}", **kw):
        args = dict(access_key=_AK, secret_key=_SK, region="ap-northeast-1", service="bedrock",
                    now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        args.update(kw)
        return sign_request("POST", url, body, **args)

    def test_signature_covers_body_region_and_path(self):
        base = _sig(self._sign())
        assert _sig(self._sign(body=b'{"x":1}')) != base
        assert _sig(self._sign(region="us-east-1")) != base
        assert _sig(self._sign(url="https://bedrock-runtime.ap-northeast-1.amazonaws.com/model/m/converse-stream")) != base

    def test_default_port_is_not_part_of_signed_host(self):
        """httpx omits :443 from the Host header, so the signature must too."""
        assert _sig(self._sign(url="https://bedrock-runtime.ap-northeast-1.amazonaws.com:443/model/m/converse")) \
            == _sig(self._sign())

    def test_secret_is_never_in_the_headers(self):
        headers = self._sign()
        assert _SK not in "".join(headers.values())
        assert f"Credential={_AK}/20260101/ap-northeast-1/bedrock/aws4_request" in headers["Authorization"]
