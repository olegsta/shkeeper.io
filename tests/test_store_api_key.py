from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from flask import Flask, g

from shkeeper.models import StoreStatus, UserRole
from shkeeper.services.store_service import (
    MIN_STORE_API_KEY_LENGTH,
    mint_store_api_key,
    resolve_store_by_api_key,
)


class TestResolveStoreByApiKey(unittest.TestCase):
    @mock.patch("shkeeper.services.store_service.Wallet")
    @mock.patch("shkeeper.services.store_service.Store")
    def test_short_keys_do_not_match(self, store_model, wallet_model) -> None:
        for api_key in ("", "a", "short-key", "x" * (MIN_STORE_API_KEY_LENGTH - 1), None, 1):
            self.assertIsNone(resolve_store_by_api_key(api_key))
        store_model.query.filter_by.assert_not_called()
        wallet_model.query.filter_by.assert_not_called()

    @mock.patch("shkeeper.services.store_service.Wallet")
    @mock.patch("shkeeper.services.store_service.Store")
    def test_long_enough_key_resolves_active_store(self, store_model, wallet_model) -> None:
        store = SimpleNamespace(id=2)
        store_model.query.filter_by.return_value.first.return_value = store
        api_key = "k" * MIN_STORE_API_KEY_LENGTH

        self.assertIs(resolve_store_by_api_key(api_key), store)
        store_model.query.filter_by.assert_called_once_with(
            api_key=api_key,
            status=StoreStatus.ACTIVE,
        )
        wallet_model.query.filter_by.assert_not_called()

    @mock.patch("shkeeper.services.store_service.ensure_default_store", return_value="default")
    @mock.patch("shkeeper.services.store_service.Wallet")
    @mock.patch("shkeeper.services.store_service.Store")
    def test_long_key_falls_back_to_legacy_wallet(
        self, store_model, wallet_model, ensure_default_store
    ) -> None:
        store_model.query.filter_by.return_value.first.return_value = None
        wallet_model.query.filter_by.return_value.first.return_value = SimpleNamespace()
        api_key = "k" * MIN_STORE_API_KEY_LENGTH

        self.assertEqual(resolve_store_by_api_key(api_key), "default")
        wallet_model.query.filter_by.assert_called_once_with(apikey=api_key)
        ensure_default_store.assert_called_once_with()


class TestMintStoreApiKey(unittest.TestCase):
    def test_minted_key_clears_the_length_floor(self) -> None:
        key = mint_store_api_key()
        self.assertGreaterEqual(len(key), MIN_STORE_API_KEY_LENGTH)
        self.assertEqual(len(key), 32)
        self.assertNotEqual(mint_store_api_key(), key)


class TestPaymentGatewaySetToken(unittest.TestCase):
    def setUp(self) -> None:
        self._flask_app = Flask(__name__)
        self._app_ctx = self._flask_app.app_context()
        self._app_ctx.push()
        self.addCleanup(self._app_ctx.pop)

    def test_store_owner_rotation_ignores_client_token(self) -> None:
        from shkeeper.api_v1 import payment_gateway_set_token

        store = SimpleNamespace(api_key="old-store-key-value")
        minted = "S" * 32
        with self._flask_app.test_request_context(
            "/api/v1/BTC/payment-gateway/token",
            method="POST",
            json={"token": ""},
        ):
            g.current_store = store
            g.user = SimpleNamespace(role=UserRole.STORE_OWNER)
            with (
                mock.patch("shkeeper.api_v1.store_owner_wallet", return_value=True),
                mock.patch("shkeeper.api_v1.mint_store_api_key", return_value=minted),
                mock.patch("shkeeper.api_v1.db") as db,
            ):
                result = payment_gateway_set_token(crypto_name="BTC")
        self.assertEqual(store.api_key, minted)
        self.assertEqual(result, {"status": "success", "token": minted})
        db.session.commit.assert_called_once_with()

    def test_admin_rotation_does_not_store_a_trivial_token(self) -> None:
        from shkeeper.api_v1 import payment_gateway_set_token

        wallets = [SimpleNamespace(apikey="old") for _ in range(2)]
        cryptos = {
            "BTC": SimpleNamespace(wallet=wallets[0]),
            "ETH": SimpleNamespace(wallet=wallets[1]),
        }
        default_store = SimpleNamespace(api_key="old-default-key")
        minted = "M" * 32
        with self._flask_app.test_request_context(
            "/api/v1/BTC/payment-gateway/token",
            method="POST",
            json={"token": "a"},
        ):
            g.user = SimpleNamespace(role=UserRole.ADMIN)
            with (
                mock.patch("shkeeper.api_v1.store_owner_wallet", return_value=None),
                mock.patch("shkeeper.api_v1.require_admin") as require_admin,
                mock.patch("shkeeper.api_v1.mint_store_api_key", return_value=minted),
                mock.patch.dict("shkeeper.api_v1.Crypto.instances", cryptos, clear=True),
                mock.patch(
                    "shkeeper.services.store_service.ensure_default_store",
                    return_value=default_store,
                ),
                mock.patch("shkeeper.api_v1.db") as db,
            ):
                result = payment_gateway_set_token(crypto_name="BTC")
        require_admin.assert_called_once_with()
        self.assertEqual(result["token"], minted)
        self.assertEqual(default_store.api_key, minted)
        self.assertTrue(all(wallet.apikey == minted for wallet in wallets))
        db.session.commit.assert_called_once_with()
