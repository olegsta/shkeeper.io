from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from flask import Flask, g
from werkzeug.exceptions import Forbidden

from shkeeper.models import UserRole


class TestPayoutDestinationsAuth(unittest.TestCase):
    def setUp(self) -> None:
        self._flask_app = Flask(__name__)
        self._app_ctx = self._flask_app.app_context()
        self._app_ctx.push()
        self.addCleanup(self._app_ctx.pop)

    def _call(self, payload, role):
        from shkeeper.api_v1 import payout_destinations

        with self._flask_app.test_request_context(
            "/api/v1/BTC/payout_destinations",
            method="POST",
            json=payload,
        ):
            g.user = SimpleNamespace(role=role)
            return payout_destinations(crypto_name="BTC")

    def test_list_rejects_store_owner(self) -> None:
        with mock.patch("shkeeper.api_v1.PayoutDestination") as destination:
            with self.assertRaises(Forbidden):
                self._call({"action": "list"}, UserRole.STORE_OWNER)
        destination.query.filter_by.assert_not_called()

    def test_list_admin_returns_destinations(self) -> None:
        dest = SimpleNamespace(addr="bc1qcold", comment="ops")
        with mock.patch("shkeeper.api_v1.PayoutDestination") as destination:
            destination.query.filter_by.return_value.all.return_value = [dest]
            response = self._call({"action": "list"}, UserRole.ADMIN)
        destination.query.filter_by.assert_called_once_with(crypto="BTC")
        self.assertEqual(
            response,
            {
                "status": "success",
                "payout_destinations": [{"addr": "bc1qcold", "comment": "ops"}],
            },
        )

    def test_missing_action_returns_400(self) -> None:
        with mock.patch("shkeeper.api_v1.PayoutDestination") as destination:
            body, status = self._call({}, UserRole.ADMIN)
        self.assertEqual(status, 400)
        self.assertEqual(body["message"], "action required")
        destination.query.filter_by.assert_not_called()

    def test_unknown_action_returns_400(self) -> None:
        body, status = self._call({"action": "export"}, UserRole.ADMIN)
        self.assertEqual(status, 400)
        self.assertEqual(body["message"], "Unknown action")

    def test_non_object_body_returns_400(self) -> None:
        body, status = self._call(["list"], UserRole.STORE_OWNER)
        self.assertEqual(status, 400)
        self.assertEqual(body["message"], "action required")
