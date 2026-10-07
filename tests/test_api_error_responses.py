"""API errors must not return Python tracebacks to clients."""

from __future__ import annotations

import unittest
from unittest import mock

from flask import Flask
from werkzeug.exceptions import abort

from shkeeper import handle_unexpected_error
from shkeeper.api_v1 import (
    decrypt_key,
    get_crypto_quote,
    list_payouts,
    payment_request,
    payoutnotify,
)


def _view(func):
    """Walk functools.wraps so tests call the view, not its auth decorator."""
    seen = set()
    while getattr(func, "__wrapped__", None) is not None and id(func) not in seen:
        seen.add(id(func))
        func = func.__wrapped__
    return func


def _assert_no_leak(test: unittest.TestCase, body, secret: str) -> None:
    rendered = str(body)
    test.assertNotIn("traceback", body)
    test.assertNotIn("Traceback", rendered)
    test.assertNotIn(secret, rendered)
    test.assertNotIn("site-packages", rendered)
    test.assertIn("error_id", body)
    test.assertTrue(body["error_id"])
    test.assertNotIn(secret, body["message"])


class _EnabledCrypto:
    crypto = "BTC"

    class wallet:
        enabled = True


class TestApiErrorResponses(unittest.TestCase):
    def setUp(self) -> None:
        self.app = Flask(__name__)
        self.app.config["PROPAGATE_EXCEPTIONS"] = False

    def test_payment_request_unknown_crypto_has_no_traceback(self) -> None:
        with self.app.test_request_context(
            "/api/v1/AAAA/payment_request", method="POST", json={}
        ):
            with mock.patch.dict(
                "shkeeper.api_v1.Crypto.instances", {}, clear=True
            ):
                body, status = _view(payment_request)("AAAA")

        self.assertEqual(status, 400)
        self.assertEqual(body["status"], "error")
        self.assertNotIn("traceback", body)
        self.assertNotIn("error_id", body)

    def test_payment_request_exception_returns_correlation_id(self) -> None:
        secret = "/shkeeper.io/shkeeper/api_v1.py"
        fake = _EnabledCrypto()
        with self.app.test_request_context(
            "/api/v1/BTC/payment_request",
            method="POST",
            json={"external_id": "1", "callback_url": "https://cb.example/hook"},
        ):
            with mock.patch.dict(
                "shkeeper.api_v1.Crypto.instances", {"BTC": fake}, clear=True
            ):
                with mock.patch(
                    "shkeeper.api_v1.Invoice.add",
                    side_effect=RuntimeError(secret),
                ):
                    with self.assertLogs(self.app.logger, level="ERROR") as logs:
                        body, status = _view(payment_request)("BTC")

        self.assertEqual(status, 500)
        self.assertEqual(body["message"], "Failed to create invoice")
        _assert_no_leak(self, body, secret)
        self.assertIn(secret, "\n".join(logs.output))
        self.assertIn(body["error_id"], "\n".join(logs.output))

    def test_quote_malformed_amount_is_400_without_traceback(self) -> None:
        fake = _EnabledCrypto()
        with self.app.test_request_context(
            "/api/v1/BTC/quote",
            method="POST",
            json={"fiat": "USD", "amount": "not-a-number"},
        ):
            with mock.patch.dict(
                "shkeeper.api_v1.Crypto.instances", {"BTC": fake}, clear=True
            ):
                body, status = _view(get_crypto_quote)("BTC")

        self.assertEqual(status, 400)
        self.assertEqual(body["message"], "Malformed quote request")
        _assert_no_leak(self, body, "not-a-number")

    def test_payoutnotify_wrong_key_does_not_leak(self) -> None:
        with self.app.test_request_context(
            "/api/v1/AAAA/payoutnotify",
            method="POST",
            data="not-json",
            content_type="application/json",
            headers={"X-Shkeeper-Backend-Key": "x"},
        ):
            body, status = payoutnotify("AAAA")

        self.assertEqual(status, 403)
        self.assertNotIn("traceback", body)
        self.assertNotIn("Traceback", str(body))

    def test_payoutnotify_unknown_crypto_does_not_parse_body(self) -> None:
        with self.app.test_request_context(
            "/api/v1/AAAA/payoutnotify",
            method="POST",
            data="not-json",
            content_type="application/json",
            headers={"X-Shkeeper-Backend-Key": "shkeeper"},
        ):
            with mock.patch.dict("shkeeper.api_v1.Crypto.instances", {}, clear=True):
                body, status = payoutnotify("AAAA")

        self.assertEqual(status, 400)
        self.assertNotIn("traceback", body)
        self.assertNotIn("site-packages", str(body))

    def test_payoutnotify_bad_json_returns_correlation_id(self) -> None:
        with self.app.test_request_context(
            "/api/v1/BTC/payoutnotify",
            method="POST",
            data="not-json",
            content_type="application/json",
            headers={"X-Shkeeper-Backend-Key": "shkeeper"},
        ):
            with mock.patch.dict(
                "shkeeper.api_v1.Crypto.instances",
                {"BTC": _EnabledCrypto()},
                clear=True,
            ):
                body, status = payoutnotify("BTC")

        self.assertEqual(status, 400)
        self.assertEqual(body["message"], "Malformed payout notification")
        _assert_no_leak(self, body, "not-json")

    def test_decrypt_key_exception_returns_correlation_id(self) -> None:
        secret = "/var/lib/shkeeper/secret"
        with self.app.test_request_context(
            "/api/v1/BTC/decrypt",
            headers={"X-Shkeeper-Backend-Key": "x"},
        ):
            with mock.patch(
                "shkeeper.api_v1.environ.get", side_effect=RuntimeError(secret)
            ):
                with self.assertLogs(self.app.logger, level="ERROR") as logs:
                    body, status = decrypt_key("BTC")

        self.assertEqual(status, 409)
        self.assertEqual(body["message"], "Failed to read wallet encryption state")
        _assert_no_leak(self, body, secret)
        self.assertIn(secret, "\n".join(logs.output))

    def test_list_payouts_missing_amount_is_client_error(self) -> None:
        with self.app.test_request_context("/api/v1/BTC/payouts"):
            body, status = _view(list_payouts)("BTC")

        self.assertEqual(status, 400)
        self.assertEqual(body["message"], "No amount provided.")
        self.assertNotIn("traceback", body)

    def test_unhandled_api_exception_hides_traceback(self) -> None:
        secret = "/opt/shkeeper/site-packages/flask"

        @self.app.get("/api/v1/boom")
        def boom():
            raise RuntimeError(secret)

        self.app.register_error_handler(Exception, handle_unexpected_error)
        response = self.app.test_client().get("/api/v1/boom")
        payload = response.get_json()

        self.assertEqual(response.status_code, 500)
        self.assertEqual(payload["message"], "Internal server error")
        _assert_no_leak(self, payload, secret)
        self.assertNotIn(secret, response.get_data(as_text=True))

    def test_http_exception_is_not_rewritten(self) -> None:
        @self.app.get("/api/v1/missing")
        def missing():
            abort(404)

        self.app.register_error_handler(Exception, handle_unexpected_error)
        response = self.app.test_client().get("/api/v1/missing")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("Traceback (most recent call last)", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
