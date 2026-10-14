from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from flask import Flask, g
from werkzeug.exceptions import Forbidden, NotFound

from shkeeper.models import UserRole
from shkeeper.wallet import admin_test_payment_request


class TestAdminTestPaymentRequest(unittest.TestCase):
    def setUp(self) -> None:
        self._flask_app = Flask(__name__)
        self._flask_app.config["DEV_MODE"] = False
        self._app_ctx = self._flask_app.app_context()
        self._app_ctx.push()
        self.addCleanup(self._app_ctx.pop)

    def _post(self, user, dev_mode=True):
        self._flask_app.config["DEV_MODE"] = dev_mode
        with self._flask_app.test_request_context(
            "/admin/test-payment-request",
            method="POST",
            data={"crypto": "BTC", "amount": "1"},
        ):
            g.user = user
            return admin_test_payment_request()

    def test_store_owner_is_forbidden(self) -> None:
        user = SimpleNamespace(id=2, role=UserRole.STORE_OWNER)
        with mock.patch("shkeeper.wallet.Invoice.add") as add_invoice:
            with self.assertRaises(Forbidden):
                self._post(user, dev_mode=True)
        add_invoice.assert_not_called()

    def test_admin_outside_dev_mode_is_not_found(self) -> None:
        user = SimpleNamespace(id=1, role=UserRole.ADMIN)
        with mock.patch("shkeeper.wallet.Invoice.add") as add_invoice:
            with self.assertRaises(NotFound):
                self._post(user, dev_mode=False)
        add_invoice.assert_not_called()

    def test_exception_is_not_returned_to_the_page(self) -> None:
        user = SimpleNamespace(id=1, role=UserRole.ADMIN)
        secret = "rpc password leaked"
        with mock.patch(
            "shkeeper.wallet.get_available_cryptos",
            return_value={"crypto_list": []},
        ):
            with mock.patch(
                "shkeeper.wallet.render_template", return_value="ok"
            ) as render:
                with mock.patch(
                    "shkeeper.wallet.Invoice.add",
                    side_effect=RuntimeError(secret),
                ):
                    with mock.patch.dict(
                        "shkeeper.wallet.Crypto.instances",
                        {"BTC": object()},
                        clear=True,
                    ):
                        with mock.patch(
                            "shkeeper.wallet.url_for",
                            return_value="http://callback",
                        ):
                            result = self._post(user, dev_mode=True)
        self.assertEqual(result, "ok")
        error = render.call_args.kwargs["error"]
        self.assertEqual(
            error,
            "Failed to create the test payment request; see server logs.",
        )
        self.assertNotIn(secret, error)
