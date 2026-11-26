from __future__ import annotations

import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from shkeeper.models import StoreWalletStatus
from shkeeper.modules.cryptos.bitcoin_lightning import BitcoinLightning
from shkeeper.services.multistore import (
    crypto_supports_multistore,
    is_multistore_backend,
    store_wallet_is_ready,
    uses_fee_deposit_account,
)
from shkeeper.services.store_service import (
    validate_fee_collection_address,
    validate_merchant_wallet_address,
)


def _bare_lightning():
    ln = BitcoinLightning.__new__(BitcoinLightning)
    ln.crypto = "BTC-LIGHTNING"
    ln.LND_REST_URL = "https://lnd:8080"
    ln._store_lnurls = {}
    ln._lnurl = "lnurl1default"
    return ln


class _AppContextTestCase(unittest.TestCase):
    def setUp(self) -> None:
        from flask import Flask

        self.app = Flask(__name__)
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self) -> None:
        self.ctx.pop()


class TestLightningMultistoreFlags(unittest.TestCase):
    def test_listed_and_has_no_fee_deposit_account(self) -> None:
        ln = _bare_lightning()
        self.assertTrue(crypto_supports_multistore("BTC-LIGHTNING"))
        self.assertTrue(is_multistore_backend(ln))
        self.assertFalse(uses_fee_deposit_account(ln))
        self.assertEqual(BitcoinLightning.network_currency, "BTC-LIGHTNING")

    def test_ready_without_fda_when_crypto_is_not_loaded(self) -> None:
        from shkeeper.modules.classes.crypto import Crypto

        sw = SimpleNamespace(
            status=StoreWalletStatus.READY,
            fda_address=None,
            crypto="BTC-LIGHTNING",
        )
        with mock.patch.dict(Crypto.instances, {}, clear=True):
            self.assertTrue(store_wallet_is_ready(sw))


class TestLnbitsWalletBalance(_AppContextTestCase):
    def test_balance_is_the_store_wallet_in_millisatoshis(self) -> None:
        ln = _bare_lightning()
        ln.prepare_store = lambda store_id=None: SimpleNamespace(invoice_key="inv")
        ln._lnbits_api = lambda *args, **kwargs: (200, {"balance": 100_000_000_000})

        self.assertEqual(ln.balance_for_account(store_id=2), Decimal("1"))

    def test_balance_is_zero_when_lnbits_rejects(self) -> None:
        ln = _bare_lightning()
        ln.prepare_store = lambda store_id=None: SimpleNamespace(invoice_key="inv")
        ln._lnbits_api = lambda *args, **kwargs: (401, {"detail": "nope"})

        self.assertEqual(ln.balance_for_account(store_id=2), Decimal(0))


class TestLnbitsWalletCreate(_AppContextTestCase):
    def test_creates_wallet_on_the_logged_in_account(self) -> None:
        ln = _bare_lightning()
        calls = []

        def api(method, path, key, payload=None, account=False):
            calls.append((method, path, payload, account))
            if method == "GET" and path.startswith("/api/v1/wallet/paginated"):
                return 200, {"data": []}
            if method == "POST" and path == "/api/v1/wallet":
                return 201, {
                    "id": "wal-2",
                    "user": "user-1",
                    "name": "shkeeper-store-2",
                    "adminkey": "adm",
                    "inkey": "inv",
                }
            raise AssertionError((method, path))

        ln._lnbits_api = api
        created = ln._create_lnbits_wallet(2)

        self.assertEqual(created["user_id"], "user-1")
        self.assertEqual(created["wallet_id"], "wal-2")
        self.assertEqual(created["admin_key"], "adm")
        self.assertEqual(created["invoice_key"], "inv")
        self.assertTrue(calls[0][3])
        self.assertTrue(calls[1][3])
        self.assertEqual(calls[1][2], {"name": "shkeeper-store-2"})
        self.assertFalse(any("/users/" in path for _, path, _, _ in calls))

    def test_reuses_existing_named_wallet(self) -> None:
        ln = _bare_lightning()

        def api(method, path, key, payload=None, account=False):
            if method == "GET" and path.startswith("/api/v1/wallet/paginated"):
                return 200, {
                    "data": [
                        {
                            "id": "wal-2",
                            "user": "user-1",
                            "name": "shkeeper-store-2",
                            "adminkey": "adm",
                            "inkey": "inv",
                        }
                    ]
                }
            raise AssertionError((method, path))

        ln._lnbits_api = api
        created = ln._create_lnbits_wallet(2)
        self.assertEqual(created["wallet_id"], "wal-2")
        self.assertEqual(created["user_id"], "user-1")


class TestLightningPayout(_AppContextTestCase):
    def test_pays_with_the_store_admin_key(self) -> None:
        ln = _bare_lightning()
        wallet = SimpleNamespace(admin_key="store-admin")
        ln.lnurl_to_pr = lambda destination, amount=None: ("lnbc1pay", None)
        ln.session = mock.Mock()
        ln.session.get.return_value.json.return_value = {
            "destination": "pubkey",
            "num_satoshis": "1000",
        }
        ln.LIGHTNING_REQUESTS_TIMEOUT = 5
        seen = {}

        def api(method, path, key, payload=None):
            seen["key"] = key
            seen["payload"] = payload
            return 201, {"payment_hash": "abc"}

        ln._lnbits_api = api
        res = ln._send_payment("user@example.com", Decimal("0.00001"), wallet)

        self.assertEqual(res["result"], "abc")
        self.assertEqual(seen["key"], "store-admin")
        self.assertEqual(seen["payload"]["bolt11"], "lnbc1pay")
        self.assertTrue(seen["payload"]["out"])

    def test_lnbits_rejection_is_returned(self) -> None:
        ln = _bare_lightning()
        wallet = SimpleNamespace(admin_key="store-admin")
        ln.lnurl_to_pr = lambda destination, amount=None: ("lnbc1pay", None)
        ln.session = mock.Mock()
        ln.session.get.return_value.json.return_value = {
            "destination": "pubkey",
            "num_satoshis": "1000",
        }
        ln.LIGHTNING_REQUESTS_TIMEOUT = 5
        ln._lnbits_api = lambda *args, **kwargs: (400, {"detail": "Insufficient balance."})

        res = ln._send_payment("user@example.com", Decimal("1"), wallet)

        self.assertIsNone(res["result"])
        self.assertIn("Insufficient balance", res["error"]["message"])

    def test_multipayout_stops_before_sending_when_the_wallet_is_short(self) -> None:
        ln = _bare_lightning()
        ln.balance_for_account = lambda store_id=None: Decimal("0.5")
        ln.mkpayout = mock.Mock(return_value={"result": "hash", "error": None})

        res = ln.multipayout(
            [
                {"dest": "a@example.com", "amount": "0.4"},
                {"dest": "b@example.com", "amount": "0.4"},
            ],
            store_id=2,
        )

        self.assertIsNone(res["result"])
        ln.mkpayout.assert_not_called()


class TestLightningAddressValidation(unittest.TestCase):
    LNURL = "lnurl1dp68gurn8ghj7mrww4exctnxd9shg6npvchxxmmd9akxuatjd3cz7cmgv4j8gctswqhxget8ypekzar0wqs"

    def test_accepts_lnurl_and_lightning_address(self) -> None:
        self.assertEqual(
            validate_fee_collection_address("BTC-LIGHTNING", self.LNURL),
            self.LNURL,
        )
        self.assertEqual(
            validate_merchant_wallet_address("BTC-LIGHTNING", "shop@pay.example"),
            "shop@pay.example",
        )

    def test_rejects_ethereum_address(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            validate_fee_collection_address("BTC-LIGHTNING", "0x" + "a" * 40)
        self.assertIn("Invalid Lightning address", str(ctx.exception))

    def test_blank_is_cleared(self) -> None:
        self.assertIsNone(validate_merchant_wallet_address("BTC-LIGHTNING", "  "))


class TestLightningProvision(unittest.TestCase):
    def test_provision_opens_an_lnbits_wallet(self) -> None:
        from flask import Flask

        from shkeeper.models import StoreWalletStatus
        from shkeeper.services.store_service import _provision_network

        app = Flask(__name__)
        with app.app_context():
            ln = BitcoinLightning.__new__(BitcoinLightning)
            ln.prepare_store = mock.Mock()
            sw = mock.Mock()
            with mock.patch("shkeeper.services.store_service.db") as db, mock.patch(
                "shkeeper.services.store_service.uses_fee_deposit_account",
                return_value=False,
            ):
                _provision_network(mock.Mock(id=3), "BTC-LIGHTNING", [(sw, ln)])

            ln.prepare_store.assert_called_once_with(3)
            self.assertEqual(sw.status, StoreWalletStatus.READY)
            db.session.commit.assert_called_once()
