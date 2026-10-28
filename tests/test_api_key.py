from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from flask import Flask, g

from shkeeper.models import StoreStatus, UserRole
from shkeeper.services.store_service import (
    API_KEY_BYTES,
    generate_api_key,
    resolve_store_by_api_key,
)


class TestGenerateApiKey(unittest.TestCase):
    def test_uses_256_bit_urlsafe_token(self) -> None:
        with mock.patch(
            "shkeeper.services.store_service.secrets.token_urlsafe",
            return_value="server-minted-key",
        ) as token_urlsafe:
            self.assertEqual(generate_api_key(), "server-minted-key")
        token_urlsafe.assert_called_once_with(API_KEY_BYTES)
        self.assertEqual(API_KEY_BYTES, 32)

    def test_keys_are_unique_and_long_enough_to_authenticate(self) -> None:
        first = generate_api_key()
        second = generate_api_key()
        self.assertNotEqual(first, second)
        self.assertGreaterEqual(len(first), 16)
        self.assertRegex(first, r"^[A-Za-z0-9_-]+$")


class TestResolveStoreByApiKey(unittest.TestCase):
    def test_blank_or_short_keys_never_match(self) -> None:
        with mock.patch("shkeeper.services.store_service.Store") as store_model:
            with mock.patch("shkeeper.services.store_service.Wallet") as wallet_model:
                for api_key in ("", None, "a", "x" * 15):
                    self.assertIsNone(resolve_store_by_api_key(api_key))
        store_model.query.filter_by.assert_not_called()
        wallet_model.query.filter_by.assert_not_called()

    def test_active_store_key_matches(self) -> None:
        store = SimpleNamespace(id=2)
        api_key = "k" * 16
        with mock.patch("shkeeper.services.store_service.Store") as store_model:
            store_model.query.filter_by.return_value.first.return_value = store
            self.assertIs(resolve_store_by_api_key(api_key), store)
        store_model.query.filter_by.assert_called_once_with(
            api_key=api_key,
            status=StoreStatus.ACTIVE,
        )

    def test_legacy_wallet_key_resolves_to_default_store(self) -> None:
        default_store = SimpleNamespace(id=1, is_default=True)
        api_key = "w" * 16
        with mock.patch("shkeeper.services.store_service.Store") as store_model:
            with mock.patch("shkeeper.services.store_service.Wallet") as wallet_model:
                with mock.patch(
                    "shkeeper.services.store_service.ensure_default_store",
                    return_value=default_store,
                ) as ensure_default_store:
                    store_model.query.filter_by.return_value.first.return_value = None
                    wallet_model.query.filter_by.return_value.first.return_value = (
                        SimpleNamespace(apikey=api_key)
                    )
                    self.assertIs(resolve_store_by_api_key(api_key), default_store)
        ensure_default_store.assert_called_once_with()
        wallet_model.query.filter_by.assert_called_once_with(apikey=api_key)


class TestPaymentGatewaySetToken(unittest.TestCase):
    def setUp(self) -> None:
        self._flask_app = Flask(__name__)
        self._app_ctx = self._flask_app.app_context()
        self._app_ctx.push()
        self.addCleanup(self._app_ctx.pop)

    def test_store_owner_rotation_ignores_client_token(self) -> None:
        from shkeeper.api_v1 import payment_gateway_set_token

        store = SimpleNamespace(id=5, api_key="old-store-key-value")
        minted = "server-minted-store-key"
        with self._flask_app.test_request_context(
            "/api/v1/ETH/payment-gateway/token",
            method="POST",
            json={"token": "a"},
        ):
            g.user = SimpleNamespace(id=9, role=UserRole.STORE_OWNER)
            g.current_store = store
            with mock.patch(
                "shkeeper.api_v1.store_owner_wallet", return_value=True
            ), mock.patch("shkeeper.api_v1.db") as db, mock.patch(
                "shkeeper.services.store_service.generate_api_key",
                return_value=minted,
            ):
                result = payment_gateway_set_token(crypto_name="ETH")
        self.assertEqual(result, {"status": "success", "token": minted})
        self.assertEqual(store.api_key, minted)
        db.session.commit.assert_called_once()

    def test_admin_rotation_replaces_wallet_and_default_store_keys(self) -> None:
        from shkeeper.api_v1 import payment_gateway_set_token

        class FakeCrypto:
            def __init__(self) -> None:
                self.wallet = SimpleNamespace(apikey="old-wallet-key")

        fake = FakeCrypto()
        default_store = SimpleNamespace(id=1, api_key="old-default-key")
        minted = "server-minted-global-key"
        with self._flask_app.test_request_context(
            "/api/v1/ETH/payment-gateway/token",
            method="POST",
            data=b'{"token":""}',
            content_type="application/json",
        ):
            g.user = SimpleNamespace(id=1, role=UserRole.ADMIN)
            with mock.patch(
                "shkeeper.api_v1.store_owner_wallet", return_value=None
            ), mock.patch("shkeeper.api_v1.require_admin") as require_admin, mock.patch(
                "shkeeper.api_v1.db"
            ) as db, mock.patch.dict(
                "shkeeper.api_v1.Crypto.instances", {"ETH": fake}, clear=True
            ), mock.patch(
                "shkeeper.services.store_service.ensure_default_store",
                return_value=default_store,
            ) as ensure_default_store, mock.patch(
                "shkeeper.services.store_service.generate_api_key",
                return_value=minted,
            ):
                result = payment_gateway_set_token(crypto_name="ETH")
        self.assertEqual(result, {"status": "success", "token": minted})
        self.assertEqual(fake.wallet.apikey, minted)
        self.assertEqual(default_store.api_key, minted)
        require_admin.assert_called_once_with()
        ensure_default_store.assert_called_once_with()
        db.session.commit.assert_called_once()
