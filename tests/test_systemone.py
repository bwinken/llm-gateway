"""
Tests for POST /v1/systemone (and the bare /systemone alias).

A System One model — e.g. Mapika's decider — answers typed questions about a
state (choice / score / yes-no "noul") with calibrated probabilities from one
forward pass. The gateway forwards TypeSafe's wire format verbatim to
``{base_url}/systemone`` on a ``systemone``-typed route, rewrites only
``model``, and bills input tokens — including the schema-cache prefix decider
reports as ``cached_tokens`` next to (not inside) ``input_tokens``.

The ``TestTypeSafeSdkContract`` class pins what the TypeSafe Python SDK
(``typesafe-sdk``) needs from the gateway: it POSTs
``{TYPESAFE_BASE_URL}/v1/systemone`` with ``Authorization: Bearer <key>`` and
requires a string ``model`` and a ``usage`` object on the response. Model
discovery is deliberately not offered: ``GET /v1/models`` lists chat models
(llm / vlm) only, so the SDK's ``client.models.list()`` is not supported.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from sqlmodel import select

from app.models.schema import UsageLog, User
from app.services.vllm_proxy import _usage_count
from tests.conftest import (
    TEST_FALLBACK_MAP,
    TEST_MODEL_ROUTING,
    auth_header,
    make_httpx_response,
    web_auth_header,
)

_SYSTEMONE_URL = "http://mock-systemone:8000/v1/systemone"

_STATE = "I was charged twice for order A-104. Please refund the duplicate."

_QUESTIONS = {
    "department": {
        "type": "choice",
        "instructions": "Which team should handle this?",
        "criteria": {"billing": "Charges, invoices", "returns": "Exchanges, refunds", "other": None},
    },
    "refund_requested": {"type": "noul", "instructions": "Does the customer ask for a refund?"},
    "frustration": {
        "type": "score",
        "instructions": "How frustrated is the customer?",
        "criteria": ["calm", "frustrated", "very frustrated"],
    },
}

_ANSWERS = {
    "department": {
        "type": "choice", "choice": "billing", "confidence": 0.56, "certainty": 0.37,
        "probabilities": {"billing": 0.56, "returns": 0.44, "other": 0.0},
    },
    "refund_requested": {"type": "noul", "noul": 0.99},
    "frustration": {
        "type": "score", "score": 1.1, "confidence": 0.5, "certainty": 0.3,
        "legend": {"0": "calm", "1": "frustrated", "2": "very frustrated"},
        "probabilities": {"0": 0.2, "1": 0.5, "2": 0.3},
    },
}


def _decider_body(usage: dict | None = None, model: str = "decider") -> dict:
    """A response shaped like decider/serve.py's /v1/systemone."""
    return {
        "model": model,
        "answers": _ANSWERS,
        "usage": {"input_tokens": 120, "output_tokens": 0} if usage is None else usage,
    }


def _capturing_post(response):
    """Mock POST that records every call (url, a snapshot of the body, headers)."""
    calls: list[dict] = []

    async def _post(url, *args, **kwargs):
        calls.append({
            "url": str(url),
            "json": dict(kwargs.get("json") or {}),
            "headers": dict(kwargs.get("headers") or {}),
        })
        if isinstance(response, Exception):
            raise response
        return response

    return _post, calls


def _post_decision(client, body: dict | None = None, path: str = "/v1/systemone", **kwargs):
    if body is None:
        body = {"model": "test-systemone", "state": _STATE, "questions": _QUESTIONS}
    kwargs.setdefault("headers", auth_header())
    return client.post(path, json=body, **kwargs)


@pytest.fixture
def second_systemone():
    """A second systemone route on its own server (restored afterwards)."""
    TEST_MODEL_ROUTING["test-systemone-b"] = {
        "base_url": "http://mock-systemone-b:8000/v1",
        "real_model": "Mapika/decider-35b-a3b",
        "api_key": "",
        "type": "systemone",
    }
    yield "http://mock-systemone-b:8000/v1/systemone"
    TEST_MODEL_ROUTING.pop("test-systemone-b", None)
    TEST_FALLBACK_MAP.pop("systemone", None)


# ---------------------------------------------------------------------------
# Forwarding
# ---------------------------------------------------------------------------


class TestSystemOneForwarding:

    def test_forwards_body_verbatim_with_real_model(self, client, test_user):
        post, calls = _capturing_post(make_httpx_response(200, _decider_body()))
        client.__httpx_mock__.post = post

        resp = _post_decision(client, {
            "model": "test-systemone",
            "state": {"ticket": {"messages": [{"from": "customer", "text": _STATE}]}},
            "questions": _QUESTIONS,
            "independent": False,
            "layout": "state_first",
        })

        assert resp.status_code == 200
        assert len(calls) == 1
        call = calls[0]
        assert call["url"] == _SYSTEMONE_URL
        assert call["json"]["model"] == "Mapika/decider-4b"
        assert call["json"]["state"] == {"ticket": {"messages": [{"from": "customer", "text": _STATE}]}}
        assert call["json"]["questions"] == _QUESTIONS
        # Fields the gateway doesn't know are still forwarded.
        assert call["json"]["independent"] is False
        assert call["json"]["layout"] == "state_first"
        assert resp.json()["answers"] == _ANSWERS
        assert "X-Model-Fallback" not in resp.headers

    def test_bare_path_alias(self, client, test_user):
        post, calls = _capturing_post(make_httpx_response(200, _decider_body()))
        client.__httpx_mock__.post = post

        resp = _post_decision(client, path="/systemone")

        assert resp.status_code == 200
        assert calls[0]["url"] == _SYSTEMONE_URL
        assert resp.json()["answers"] == _ANSWERS

    def test_response_model_is_the_alias(self, client, test_user):
        """decider echoes its own name ("decider"); the caller sees its alias."""
        client.__httpx_mock__.post, _ = _capturing_post(make_httpx_response(200, _decider_body()))

        resp = _post_decision(client)

        assert resp.json()["model"] == "test-systemone"

    def test_no_authorization_header_when_route_has_no_key(self, client, test_user):
        post, calls = _capturing_post(make_httpx_response(200, _decider_body()))
        client.__httpx_mock__.post = post

        _post_decision(client)

        assert "Authorization" not in calls[0]["headers"]

    def test_route_api_key_sent_as_bearer(self, client, test_user):
        post, calls = _capturing_post(make_httpx_response(200, _decider_body()))
        client.__httpx_mock__.post = post

        with patch.dict(TEST_MODEL_ROUTING["test-systemone"], {"api_key": "DECIDER_KEY"}):
            _post_decision(client)

        assert calls[0]["headers"]["Authorization"] == "Bearer DECIDER_KEY"


class TestSystemOneModelResolution:

    @pytest.mark.parametrize("model", [None, ""], ids=["null", "empty"])
    def test_absent_model_uses_default_without_fallback(self, client, test_user, model):
        """`model` is optional in the System One wire format — leaving it out
        is not an unknown alias, so no X-Model-Fallback header."""
        post, calls = _capturing_post(make_httpx_response(200, _decider_body()))
        client.__httpx_mock__.post = post

        resp = _post_decision(client, {"model": model, "state": _STATE, "questions": _QUESTIONS})

        assert resp.status_code == 200
        assert calls[0]["url"] == _SYSTEMONE_URL
        assert "X-Model-Fallback" not in resp.headers
        assert resp.json()["model"] == "test-systemone"

    def test_missing_model_key_uses_default(self, client, test_user):
        post, calls = _capturing_post(make_httpx_response(200, _decider_body()))
        client.__httpx_mock__.post = post

        resp = _post_decision(client, {"state": _STATE, "questions": _QUESTIONS})

        assert resp.status_code == 200
        assert calls[0]["url"] == _SYSTEMONE_URL
        assert "X-Model-Fallback" not in resp.headers

    def test_configured_fallback_is_the_default(self, client, test_user, second_systemone):
        TEST_FALLBACK_MAP["systemone"] = "test-systemone-b"
        post, calls = _capturing_post(make_httpx_response(200, _decider_body()))
        client.__httpx_mock__.post = post

        resp = _post_decision(client, {"state": _STATE, "questions": _QUESTIONS})

        assert resp.status_code == 200
        assert calls[0]["url"] == second_systemone
        assert resp.json()["model"] == "test-systemone-b"
        assert "X-Model-Fallback" not in resp.headers

    def test_unknown_alias_falls_back_to_a_systemone_route(self, client, test_user):
        """The TypeSafe SDK sends "jev-latest" unless told otherwise — it must
        still land on a systemone server, flagged as a fallback."""
        post, calls = _capturing_post(make_httpx_response(200, _decider_body()))
        client.__httpx_mock__.post = post

        resp = _post_decision(client, {"model": "jev-latest", "state": _STATE, "questions": _QUESTIONS})

        assert resp.status_code == 200
        assert calls[0]["url"] == _SYSTEMONE_URL
        assert "X-Model-Fallback" in resp.headers
        assert resp.json()["model"] == "test-systemone"

    def test_llm_alias_is_not_sent_to_the_llm_server(self, client, test_user):
        post, calls = _capturing_post(make_httpx_response(200, _decider_body()))
        client.__httpx_mock__.post = post

        resp = _post_decision(client, {"model": "test-llm", "state": _STATE, "questions": _QUESTIONS})

        assert resp.status_code == 200
        assert calls[0]["url"] == _SYSTEMONE_URL
        assert "X-Model-Fallback" in resp.headers

    def test_systemone_alias_is_not_served_by_chat(self, client, test_user):
        """The reverse isolation: a systemone alias on /v1/chat/completions is
        a wrong-type alias there and goes to a chat model, never to decider."""
        post, calls = _capturing_post(make_httpx_response(200, {
            "id": "c1",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }))
        client.__httpx_mock__.post = post

        resp = client.post(
            "/v1/chat/completions",
            json={"model": "test-systemone", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )

        assert resp.status_code == 200
        assert "mock-systemone" not in calls[0]["url"]


# ---------------------------------------------------------------------------
# Billing
# ---------------------------------------------------------------------------


class TestSystemOneBilling:

    def test_input_tokens_billed(self, client, db_session, test_user):
        client.__httpx_mock__.post, _ = _capturing_post(
            make_httpx_response(200, _decider_body({"input_tokens": 120, "output_tokens": 0}))
        )

        assert _post_decision(client).status_code == 200

        log = db_session.exec(select(UsageLog)).one()
        assert log.model == "test-systemone"
        assert log.model_type == "systemone"
        assert log.endpoint == "/v1/systemone"
        assert log.backend == "vllm"
        assert log.input_tokens == 120
        assert log.output_tokens == 0
        # [pricing.systemone] = $0.05 / 1M input tokens
        assert float(log.cost_usd) == pytest.approx(120 * 0.05 / 1_000_000)

    def test_bare_path_logs_canonical_endpoint(self, client, db_session, test_user):
        client.__httpx_mock__.post, _ = _capturing_post(make_httpx_response(200, _decider_body()))

        assert _post_decision(client, path="/systemone").status_code == 200

        assert db_session.exec(select(UsageLog)).one().endpoint == "/v1/systemone"

    def test_cached_prefix_billed_as_input_at_full_price_by_default(self, client, db_session, test_user):
        """decider's schema-cache layout: input_tokens counts only the state,
        cached_tokens the cached question prefix — both are input."""
        client.__httpx_mock__.post, _ = _capturing_post(make_httpx_response(200, _decider_body(
            {"input_tokens": 40, "cached_tokens": 200, "output_tokens": 0}
        )))

        assert _post_decision(client).status_code == 200

        log = db_session.exec(select(UsageLog)).one()
        assert log.input_tokens == 240
        assert float(log.cost_usd) == pytest.approx(240 * 0.05 / 1_000_000)

    def test_cached_price_override_discounts_the_cached_prefix(self, client, db_session, test_user):
        client.__httpx_mock__.post, _ = _capturing_post(make_httpx_response(200, _decider_body(
            {"input_tokens": 40, "cached_tokens": 200, "output_tokens": 0}
        )))
        prices = {"input_price_per_1m": 1.0, "output_price_per_1m": 0.0, "cached_input_price_per_1m": 0.1}

        with patch.dict(TEST_MODEL_ROUTING["test-systemone"], prices):
            assert _post_decision(client).status_code == 200

        log = db_session.exec(select(UsageLog)).one()
        assert log.input_tokens == 240
        assert float(log.cost_usd) == pytest.approx((40 * 1.0 + 200 * 0.1) / 1_000_000)

    @pytest.mark.parametrize("usage", [None, {}, {"input_tokens": "lots"}], ids=["absent", "empty", "malformed"])
    def test_missing_or_malformed_usage_still_answers(self, client, db_session, test_user, usage):
        body = _decider_body()
        if usage is None:
            del body["usage"]
        else:
            body["usage"] = usage
        client.__httpx_mock__.post, _ = _capturing_post(make_httpx_response(200, body))

        resp = _post_decision(client)

        assert resp.status_code == 200
        assert resp.json()["answers"] == _ANSWERS
        assert db_session.exec(select(UsageLog)).one().input_tokens == 0


class TestUsageCount:

    @pytest.mark.parametrize("raw, expected", [
        (12, 12), ("12", 12), (7.9, 7), (None, 0), ("lots", 0), (-5, 0), ([1], 0),
        (float("inf"), 0), (float("nan"), 0),
    ])
    def test_coercion(self, raw, expected):
        assert _usage_count({"input_tokens": raw}, "input_tokens") == expected

    def test_absent_key(self):
        assert _usage_count({}, "cached_tokens") == 0


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TestSystemOneErrors:

    @pytest.mark.parametrize("status, detail", [
        (422, "choice criteria: a map of 2..255 options"),
        (413, "too many questions: 300 scoring rows, limit 256"),
    ])
    def test_downstream_error_propagated_and_not_billed(self, client, db_session, test_user, status, detail):
        client.__httpx_mock__.post, _ = _capturing_post(make_httpx_response(status, {"detail": detail}))

        resp = _post_decision(client)

        assert resp.status_code == status
        assert resp.json() == {"detail": detail}
        assert db_session.exec(select(UsageLog)).all() == []

    def test_busy_server_fails_over_to_another_systemone_server(self, client, test_user, second_systemone):
        """decider answers 503 when its queue is full; another alive systemone
        server takes the request."""
        calls: list[str] = []

        async def _post(url, *args, **kwargs):
            calls.append(str(url))
            if str(url) == _SYSTEMONE_URL:
                return make_httpx_response(503, {"detail": "server busy: 4096 rows queued; retry later"})
            return make_httpx_response(200, _decider_body())

        client.__httpx_mock__.post = _post

        resp = _post_decision(client)

        assert resp.status_code == 200
        assert calls == [_SYSTEMONE_URL, second_systemone]
        assert "failover: HTTP 503" in resp.headers["X-Model-Fallback"]
        assert resp.json()["model"] == "test-systemone-b"

    def test_downstream_exception_is_502(self, client, test_user):
        client.__httpx_mock__.post, _ = _capturing_post(Exception("connection reset"))

        resp = _post_decision(client)

        assert resp.status_code == 502

    def test_non_json_200_is_502(self, client, db_session, test_user):
        client.__httpx_mock__.post, _ = _capturing_post(make_httpx_response(200, text="<html>oops</html>"))

        resp = _post_decision(client)

        assert resp.status_code == 502
        assert db_session.exec(select(UsageLog)).all() == []

    def test_non_object_json_200_is_502(self, client, db_session, test_user):
        client.__httpx_mock__.post, _ = _capturing_post(make_httpx_response(200, [1, 2, 3]))

        resp = _post_decision(client)

        assert resp.status_code == 502
        assert db_session.exec(select(UsageLog)).all() == []

    def test_malformed_json_body_is_400(self, client, test_user):
        resp = client.post(
            "/v1/systemone",
            content=b"{not json",
            headers={**auth_header(), "Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_non_object_body_is_400(self, client, test_user):
        resp = client.post("/v1/systemone", json=[_STATE], headers=auth_header())
        assert resp.status_code == 400

    def test_401_without_auth(self, client):
        resp = client.post("/v1/systemone", json={"state": _STATE, "questions": _QUESTIONS})
        assert resp.status_code in (401, 403)

    def test_x_api_key_auth(self, client, test_user):
        client.__httpx_mock__.post, _ = _capturing_post(make_httpx_response(200, _decider_body()))

        resp = client.post(
            "/v1/systemone",
            json={"state": _STATE, "questions": _QUESTIONS},
            headers={"x-api-key": "sk-testkey123"},
        )

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Observability (Langfuse)
# ---------------------------------------------------------------------------


class TestSystemOneObservability:

    def _post_observed(self, client, capture_io: bool):
        records = []
        client.__httpx_mock__.post, _ = _capturing_post(make_httpx_response(200, _decider_body(
            {"input_tokens": 40, "cached_tokens": 200, "output_tokens": 0}
        )))
        with patch("app.services.vllm_proxy.get_langfuse", return_value=MagicMock()), \
             patch("app.services.vllm_proxy.record_generation", records.append), \
             patch("app.services.vllm_proxy.capture_io_enabled", return_value=capture_io):
            assert _post_decision(client).status_code == 200
        assert len(records) == 1
        return records[0]

    def test_generation_carries_type_and_cache_usage(self, client, test_user):
        rec = self._post_observed(client, capture_io=False)

        assert rec.model_type == "systemone"
        assert rec.endpoint == "/v1/systemone"
        assert rec.model_alias == "test-systemone"
        assert rec.usage == {"input": 240, "output": 0, "cache_read_input_tokens": 200}
        # output_tokens is 0 by design — not an "empty turn" like a chat model's.
        assert rec.empty_turn is False
        assert rec.input_payload is None and rec.output_payload is None

    def test_io_captured_in_the_endpoints_own_shape(self, client, test_user):
        rec = self._post_observed(client, capture_io=True)

        assert rec.input_payload == {"state": _STATE, "questions": _QUESTIONS}
        assert rec.output_payload == _ANSWERS

    def test_downstream_error_recorded(self, client, test_user):
        records = []
        client.__httpx_mock__.post, _ = _capturing_post(make_httpx_response(422, {"detail": "bad"}))
        with patch("app.services.vllm_proxy.get_langfuse", return_value=MagicMock()), \
             patch("app.services.vllm_proxy.record_generation", records.append):
            assert _post_decision(client).status_code == 422

        assert len(records) == 1
        assert records[0].is_error is True
        assert records[0].endpoint == "/v1/systemone"


# ---------------------------------------------------------------------------
# TypeSafe Python SDK contract
# ---------------------------------------------------------------------------


class TestTypeSafeSdkContract:
    """What typesafe-sdk needs from the gateway (checked against 0.7.1)."""

    def test_sdk_request_shape_is_accepted(self, client, test_user):
        """The SDK always sends state/model/questions, Bearer auth, and its
        own identification headers; its default model is "jev-latest"."""
        post, calls = _capturing_post(make_httpx_response(200, _decider_body()))
        client.__httpx_mock__.post = post

        resp = client.post(
            "/v1/systemone",
            json={"state": _STATE, "model": "jev-latest", "questions": _QUESTIONS},
            headers={
                "Authorization": "Bearer sk-testkey123",
                "Accept": "application/json",
                "User-Agent": "typesafe-sdk/0.7.1",
                "X-TypeSafe-SDK": "typesafe-sdk/0.7.1",
            },
        )

        assert resp.status_code == 200
        assert calls[0]["url"] == _SYSTEMONE_URL

    def test_response_has_what_system_one_response_requires(self, client, test_user):
        """SystemOneResponse: `model` (str) and `usage` (object) are required;
        answers are validated by their `type` discriminator."""
        client.__httpx_mock__.post, _ = _capturing_post(make_httpx_response(200, _decider_body()))

        data = _post_decision(client).json()

        assert isinstance(data["model"], str)
        assert isinstance(data["usage"], dict)
        assert isinstance(data["usage"]["input_tokens"], int)
        assert {a["type"] for a in data["answers"].values()} == {"choice", "noul", "score"}

    def test_models_list_stays_chat_only(self, client, test_user):
        """No model discovery for System One: /v1/models lists llm / vlm only
        and carries no TypeSafe-shaped `models` list, so the SDK's
        client.models.list() is intentionally unsupported."""
        data = client.get("/v1/models", headers=auth_header()).json()

        assert set(data) == {"object", "data"}
        assert {m["type"] for m in data["data"]} <= {"llm", "vlm"}
        assert "test-systemone" not in {m["id"] for m in data["data"]}


# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------


class TestSystemOnePages:

    def _page(self, client, db_session, path, username):
        db_session.add(User(username=username))
        db_session.commit()
        resp = client.get(path, headers=web_auth_header(sub=username, scopes=["read"]))
        assert resp.status_code == 200
        return resp.text

    def test_dashboard_shows_sdk_example_for_configured_alias(self, client, db_session):
        body = self._page(client, db_session, "/dashboard", "sdkcard")

        assert "TypeSafeClient(" in body
        assert "POST /v1/systemone" in body
        # The SDK appends /v1/systemone itself: base_url is the gateway root.
        assert '"http://testserver"' in body
        assert '"http://testserver/v1"' not in body.split("TypeSafeClient(", 1)[1].split("</pre>", 1)[0]
        assert '"test-systemone"' in body

    def test_dashboard_hides_sdk_example_without_systemone_models(self, client, db_session):
        with patch.dict(TEST_MODEL_ROUTING):
            del TEST_MODEL_ROUTING["test-systemone"]
            body = self._page(client, db_session, "/dashboard", "nocard")

        assert "TypeSafeClient(" not in body

    def test_welcome_lists_the_endpoint(self, client, db_session):
        body = self._page(client, db_session, "/", "welcomer")

        assert "/v1/systemone" in body
