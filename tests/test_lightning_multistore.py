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

    def test_default_store_adopts_the_legacy_wallet_before_login(self) -> None:
        ln = _bare_lightning()
        ln._legacy_default_wallet = lambda: {
            "id": "old",
            "user": "user-1",
            "adminkey": "old-adm",
            "inkey": "old-inv",
        }
        ln._account_wallets = mock.Mock(side_effect=AssertionError("must not log in"))

        created = ln._create_lnbits_wallet(1)

        self.assertEqual(created["wallet_id"], "old")
        ln._account_wallets.assert_not_called()


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

        res = ln._send_payment("user@example.com", Decimal("0.00001"), wallet)

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

    def test_fixed_invoice_must_match_the_transfer_amount(self) -> None:
        ln = _bare_lightning()
        wallet = SimpleNamespace(admin_key="store-admin")
        ln.lnurl_to_pr = lambda destination, amount=None: ("lntb10u", None)
        ln.session = mock.Mock()
        ln.session.get.return_value.json.return_value = {
            "destination": "pubkey",
            "num_satoshis": "1000",
        }
        ln.LIGHTNING_REQUESTS_TIMEOUT = 5
        ln._lnbits_api = mock.Mock()

        res = ln._send_payment("lntb10u", Decimal("0.00000010"), wallet)

        self.assertIsNone(res["result"])
        self.assertIn("1000 sats", res["error"]["message"])
        ln._lnbits_api.assert_not_called()

    def test_amountless_invoice_is_rejected_before_payment(self) -> None:
        ln = _bare_lightning()
        wallet = SimpleNamespace(admin_key="store-admin")
        ln.lnurl_to_pr = lambda destination, amount=None: ("lntb1", None)
        ln.session = mock.Mock()
        ln.session.get.return_value.json.return_value = {
            "destination": "pubkey",
            "num_satoshis": "0",
        }
        ln.LIGHTNING_REQUESTS_TIMEOUT = 5
        ln._lnbits_api = mock.Mock()

        res = ln._send_payment("lntb1", Decimal("0.00000010"), wallet)

        self.assertIsNone(res["result"])
        self.assertIn("Amountless invoices are not supported", res["error"]["message"])
        ln._lnbits_api.assert_not_called()

    def test_pending_payment_is_not_reported_as_settled(self) -> None:
        ln = _bare_lightning()
        wallet = SimpleNamespace(admin_key="store-admin", invoice_key="inv")
        ln.lnurl_to_pr = lambda destination, amount=None: ("lntb10u", None)
        ln.session = mock.Mock()
        ln.session.get.return_value.json.return_value = {
            "destination": "pubkey",
            "num_satoshis": "1000",
            "payment_hash": "aa" * 32,
        }
        ln.LIGHTNING_REQUESTS_TIMEOUT = 5
        ln._lnbits_api = lambda *args, **kwargs: (
            201,
            {"payment_hash": "aa" * 32, "pending": True},
        )

        res = ln._send_payment("lntb10u", Decimal("0.00001"), wallet)

        self.assertEqual(res["result"], "aa" * 32)
        self.assertTrue(res["pending"])
        self.assertIsNone(res["error"])

    def test_confirmations_follow_the_store_payment_status(self) -> None:
        ln = _bare_lightning()
        ln.prepare_store = lambda store_id=None: SimpleNamespace(invoice_key="inv")
        seen = {}

        def api(method, path, key, payload=None, account=False):
            seen["path"] = path
            seen["key"] = key
            return 200, {"status": "pending", "payment_hash": "aa" * 32}

        ln._lnbits_api = api
        self.assertEqual(ln.get_confirmations_by_txid("aa" * 32, store_id=4), 0)
        self.assertEqual(seen["key"], "inv")
        ln._lnbits_api = lambda *args, **kwargs: (200, {"status": "success"})
        self.assertEqual(ln.get_confirmations_by_txid("aa" * 32, store_id=4), 999)
        ln._lnbits_api = lambda *args, **kwargs: (200, {"status": "failed"})
        self.assertEqual(ln.get_confirmations_by_txid("aa" * 32, store_id=4), -1)
        ln._lnbits_api = lambda *args, **kwargs: (
            200,
            {"paid": True, "details": {"pending": True, "payment_hash": "aa" * 32}},
        )
        self.assertEqual(ln.get_confirmations_by_txid("aa" * 32, store_id=4), 999)

    def test_legacy_lookup_failure_is_not_treated_as_a_missing_wallet(self) -> None:
        ln = _bare_lightning()
        ln.lnbits_admin_apikey = "legacy-key"
        ln._lnbits_api = lambda *args, **kwargs: (500, {"detail": "down"})

        with self.assertRaises(RuntimeError):
            ln._legacy_default_wallet()

    def test_internal_lnbits_payment_marks_the_invoice_settled(self) -> None:
        ln = _bare_lightning()
        ln._load_store_wallet = lambda store_id: SimpleNamespace(invoice_key="inv")
        ln._lnbits_api = lambda *args, **kwargs: (
            200,
            {"paid": True, "details": {"bolt11": "lntb1", "time": 9}},
        )
        inv = mock.Mock(r_hash="ab" * 32, store_id=2)

        self.assertTrue(ln._mark_lnbits_settlement(inv))
        inv.update.assert_called_once_with(
            r_hash="ab" * 32,
            state="SETTLED",
            payment_request="lntb1",
            settle_date="9",
        )

    def test_single_payout_reports_pending(self) -> None:
        ln = _bare_lightning()
        ln.balance_for_account = lambda store_id=None: Decimal("1")
        ln._decoded_payment_request = lambda dest, amount: (
            "lntb10u",
            {"destination": "pubkey", "num_satoshis": "1000", "payment_hash": "aa" * 32},
        )
        ln.mkpayout = lambda *args, **kwargs: {
            "result": "aa" * 32,
            "error": None,
            "pending": True,
        }

        res = ln.multipayout([{"dest": "lntb10u", "amount": "0.00001"}], store_id=1)

        self.assertTrue(res["pending"])
        self.assertIsNone(res["error"])

    def test_multipayout_rejects_a_mismatched_invoice_before_paying(self) -> None:
        ln = _bare_lightning()
        ln.balance_for_account = lambda store_id=None: Decimal("1")
        ln._decoded_payment_request = lambda dest, amount: (
            "lntb10u",
            {"destination": "pubkey", "num_satoshis": "1000"},
        )
        ln.mkpayout = mock.Mock()

        res = ln.multipayout(
            [{"dest": "lntb10u", "amount": "0.00000010"}],
            store_id=1,
        )

        self.assertIsNone(res["result"])
        self.assertEqual(res["results"], [])
        ln.mkpayout.assert_not_called()

    def test_default_store_reuses_the_legacy_lnbits_wallet(self) -> None:
        ln = _bare_lightning()
        ln._account_wallets = lambda name: []
        ln._legacy_default_wallet = lambda: {
            "id": "old",
            "user": "user-1",
            "name": "LNbits wallet",
            "adminkey": "old-adm",
            "inkey": "old-inv",
        }
        ln._lnbits_api = mock.Mock(side_effect=AssertionError("must not create"))

        created = ln._create_lnbits_wallet(1)

        self.assertEqual(created["wallet_id"], "old")
        self.assertEqual(created["admin_key"], "old-adm")
        self.assertEqual(created["invoice_key"], "old-inv")
        ln._lnbits_api.assert_not_called()


class TestLightningPartialPayoutRecords(_AppContextTestCase):
    def test_rows_after_a_failed_payment_are_marked_not_sent(self) -> None:
        from shkeeper.services.payout_service import PayoutService

        failed = []
        created = []

        def add_failed(dest, amount, crypto, error=None, **kwargs):
            failed.append((dest, error))
            return SimpleNamespace(id=len(failed))

        def create_payout_record(req, crypto_name, task_id=None, txids=None, store_id=None):
            created.append(req["dest"])
            return SimpleNamespace(id=1)

        with mock.patch.object(PayoutService, "get_crypto", return_value=mock.Mock()), mock.patch.object(
            PayoutService,
            "_multipayout_via_crypto",
            return_value={
                "result": ["h1"],
                "error": {"message": "no route"},
                "results": [
                    {"dest": "a", "result": "h1", "error": None},
                    {"dest": "b", "result": None, "error": {"message": "no route"}},
                ],
            },
        ), mock.patch(
            "shkeeper.services.payout_service.Payout.add_failed",
            side_effect=add_failed,
        ), mock.patch.object(
            PayoutService,
            "create_payout_record",
            side_effect=create_payout_record,
        ):
            PayoutService.multiple_payout(
                "BTC",
                [
                    {"dest": "a", "destination": "a", "amount": "1"},
                    {"dest": "b", "destination": "b", "amount": "1"},
                    {"dest": "c", "destination": "c", "amount": "1"},
                ],
                source={},
                enforce_destination=False,
                apply_platform_fee=False,
            )

        self.assertEqual(created, ["a"])
        self.assertEqual(failed[0][0], "b")
        self.assertEqual(failed[1][0], "c")
        self.assertEqual(failed[1][1], {"message": "Not sent"})

    def test_lightning_saves_the_invoice_hash_before_each_payment(self) -> None:
        from shkeeper.services.payout_service import PayoutService

        order = []

        def create_payout_record(req, crypto_name, task_id=None, txids=None, store_id=None):
            order.append(("save", req["dest"], list(txids or [])))
            return SimpleNamespace(
                id=len(order),
                status=None,
                success=None,
                error=None,
                transactions=[SimpleNamespace(txid=tx) for tx in (txids or [])],
            )

        def mkpayout(dest, amount, fee, **kwargs):
            order.append(("pay", dest))
            if dest == "b":
                return {"result": None, "error": {"message": "no route"}}
            return {"result": "ha", "error": None, "pending": False}

        crypto = mock.Mock()
        crypto.prepare_payouts.return_value = (
            [
                {
                    "dest": "a",
                    "amount": Decimal("1"),
                    "bolt11": "b1",
                    "encoded_sats": 1,
                    "payment_hash": "ha",
                },
                {
                    "dest": "b",
                    "amount": Decimal("1"),
                    "bolt11": "b2",
                    "encoded_sats": 1,
                    "payment_hash": "hb",
                },
                {
                    "dest": "c",
                    "amount": Decimal("1"),
                    "bolt11": "b3",
                    "encoded_sats": 1,
                    "payment_hash": "hc",
                },
            ],
            None,
        )
        crypto.mkpayout.side_effect = mkpayout
        failed = []

        with mock.patch.object(PayoutService, "get_crypto", return_value=crypto), mock.patch.object(
            PayoutService,
            "create_payout_record",
            side_effect=create_payout_record,
        ), mock.patch(
            "shkeeper.services.payout_service.Payout.add_failed",
            side_effect=lambda dest, amount, crypto, error=None, **kwargs: failed.append(
                (dest, error)
            )
            or SimpleNamespace(id=9),
        ), mock.patch("shkeeper.services.payout_service.db"):
            PayoutService.multiple_payout(
                "BTC-LIGHTNING",
                [
                    {"dest": "a", "destination": "a", "amount": "1"},
                    {"dest": "b", "destination": "b", "amount": "1"},
                    {"dest": "c", "destination": "c", "amount": "1"},
                ],
                source={},
                enforce_destination=False,
                apply_platform_fee=False,
            )

        self.assertEqual(order[0], ("save", "a", ["ha"]))
        self.assertEqual(order[1], ("pay", "a"))
        self.assertEqual(order[2], ("save", "b", ["hb"]))
        self.assertEqual(order[3], ("pay", "b"))
        self.assertNotIn(("pay", "c"), order)
        self.assertEqual(failed, [("c", {"message": "Not sent"})])

    def test_gross_amount_leaves_the_invoice_face_after_the_fee(self) -> None:
        from shkeeper.services.payout_service import PayoutService

        gross = PayoutService.gross_amount_for_net(Decimal("0.00001000"), Decimal("1"))
        self.assertEqual(gross, Decimal("0.00001010"))
        fee = (gross * Decimal("1") / Decimal(100)).quantize(Decimal("0.00000001"))
        self.assertEqual(gross - fee, Decimal("0.00001000"))
        one_sat = PayoutService.gross_amount_for_net(Decimal("0.00000001"), Decimal("1"))
        self.assertEqual(one_sat, Decimal("0.00000001"))

    def test_single_lightning_payout_is_recorded_with_the_store(self) -> None:
        from shkeeper.services.payout_service import PayoutService

        store = SimpleNamespace(id=4, is_default=False)
        with mock.patch.object(PayoutService, "get_crypto", return_value=mock.Mock()), mock.patch.object(
            PayoutService, "_store_context", return_value=(store, {"store_id": 4})
        ), mock.patch.object(PayoutService, "check_external_id_unique"), mock.patch.object(
            PayoutService,
            "enforce_store_owner_destination",
            side_effect=lambda crypto_name, destination, store=None: destination,
        ), mock.patch.object(
            PayoutService,
            "multiple_payout",
            return_value={"result": "h", "error": None},
        ) as batch:
            PayoutService.single_payout(
                "BTC-LIGHTNING",
                {"destination": "shop@pay.example", "amount": "1", "fee": "0"},
                apply_platform_fee=False,
            )

        self.assertEqual(batch.call_args.kwargs["source"], {"store_id": 4})
        self.assertEqual(batch.call_args.args[1][0]["dest"], "shop@pay.example")

    def test_unknown_payment_result_stays_in_progress(self) -> None:
        from shkeeper.models import PayoutStatus
        from shkeeper.services.payout_service import PayoutService

        payout = SimpleNamespace(
            id=1,
            status=PayoutStatus.IN_PROGRESS,
            success=None,
            error=None,
            transactions=[],
        )
        with mock.patch("shkeeper.services.payout_service.db"):
            PayoutService._apply_lightning_result(
                payout,
                {"result": None, "error": {"message": "timed out"}, "unknown": True},
            )

        self.assertEqual(payout.status, PayoutStatus.IN_PROGRESS)
        self.assertIsNone(payout.success)


class TestLightningAddressValidation(unittest.TestCase):
    LNURL = "lnurl1dp68gurn8ghj7mrww4exctnxd9shg6npvchxxmmd9akxuatjd3cz7cmgv4j8gctswqhxget8ypekzar0wqs"

    def test_lightning_address_is_resolved_to_an_invoice(self) -> None:
        from shkeeper.modules.cryptos.bitcoin_lightning import _lnurl_callback_with_amount

        ln = _bare_lightning()
        ln.LIGHTNING_REQUESTS_TIMEOUT = 5
        well_known = mock.Mock()
        well_known.raise_for_status.return_value = None
        well_known.json.return_value = {
            "tag": "payRequest",
            "callback": "https://pay.example/cb?tag=payRequest",
            "minSendable": 1000,
        }
        callback = mock.Mock()
        callback.json.return_value = {"pr": "lntb1invoice"}
        with mock.patch(
            "shkeeper.modules.cryptos.bitcoin_lightning.requests.get",
            side_effect=[well_known, callback],
        ) as get:
            invoice, info = ln.lnurl_to_pr("shop@pay.example", 1000)
        seen = [call.args[0] for call in get.call_args_list]

        self.assertEqual(invoice, "lntb1invoice")
        self.assertEqual(info["callback"], "https://pay.example/cb?tag=payRequest")
        self.assertEqual(seen[0], "https://pay.example/.well-known/lnurlp/shop")
        self.assertEqual(
            seen[1],
            _lnurl_callback_with_amount("https://pay.example/cb?tag=payRequest", 1000),
        )
        self.assertIn("&amount=1000", seen[1])

    def test_lnurl_range_is_grossed_up_for_the_fee(self) -> None:
        from shkeeper.services.payout_service import PayoutService

        low, high = PayoutService.gross_bounds_for_msat(1_000_000, 2_000_000, Decimal("1"))
        self.assertEqual(low, Decimal("0.00001010"))
        self.assertGreater(high, low)

    def test_store_wallet_save_keeps_the_merchant_address(self) -> None:
        from shkeeper.services.store_service import update_store_wallet_destinations

        sw = SimpleNamespace(
            crypto="BTC-LIGHTNING",
            cold_wallet_address=None,
            fee_collection_address=None,
        )
        update_store_wallet_destinations(
            sw, "  shop@pay.example ", " admin@pay.example "
        )
        self.assertEqual(sw.cold_wallet_address, "shop@pay.example")
        self.assertEqual(sw.fee_collection_address, "admin@pay.example")

    def test_accepts_lnurl_and_lightning_address(self) -> None:
        self.assertEqual(
            validate_fee_collection_address("BTC-LIGHTNING", self.LNURL),
            self.LNURL,
        )
        self.assertEqual(
            validate_merchant_wallet_address("BTC-LIGHTNING", "shop@pay.example"),
            "shop@pay.example",
        )

    def test_lightning_destination_is_stored_as_entered(self) -> None:
        invoice = "lntb1" + "a" * 20
        self.assertEqual(validate_fee_collection_address("BTC-LIGHTNING", invoice), invoice)
        self.assertEqual(validate_merchant_wallet_address("BTC-LIGHTNING", "0x" + "a" * 40), "0x" + "a" * 40)

    def test_legacy_wallet_is_adopted_from_the_admin_key(self) -> None:
        ln = _bare_lightning()
        ln.lnbits_admin_apikey = "legacy-admin"

        def api(method, path, key, payload=None, account=False):
            self.assertFalse(account)
            self.assertEqual((method, path, key), ("GET", "/api/v1/wallet", "legacy-admin"))
            return 200, {"id": "old", "name": "LNbits wallet", "balance": 5}

        ln._lnbits_api = api
        wallet = ln._legacy_default_wallet()
        self.assertEqual(wallet["id"], "old")
        self.assertEqual(wallet["adminkey"], "legacy-admin")
        self.assertEqual(wallet["inkey"], "legacy-admin")
        self.assertEqual(wallet["user"], "old")

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
