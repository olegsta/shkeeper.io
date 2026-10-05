from __future__ import annotations

import unittest
from unittest import mock

from decimal import Decimal

from shkeeper.services.store_service import (
    _coerce_fee_percent,
    _is_valid_eth_address,
    _is_valid_tron_address,
    _is_valid_utxo_address,
    validate_fee_collection_address,
    validate_merchant_wallet_address,
)


class TestValidateFeeCollectionAddressFormat(unittest.TestCase):
    TRON_ADDR = "TH7LCv3BMwqQSj2hkoXLaNZmVPXrSVt6yG"
    ETH_ADDR = "0x" + "a" * 40

    def test_empty_is_none(self) -> None:
        self.assertIsNone(validate_fee_collection_address("ETH", None))
        self.assertIsNone(validate_fee_collection_address("TRX", ""))
        self.assertIsNone(validate_fee_collection_address("USDT", "   "))

    def test_tron_address_rejected_for_ethereum(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            validate_fee_collection_address("ETH", self.TRON_ADDR)
        self.assertIn("Invalid Ethereum address", str(ctx.exception))
        self.assertIn(self.TRON_ADDR, str(ctx.exception))

    def test_ethereum_address_rejected_for_tron(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            validate_fee_collection_address("TRX", self.ETH_ADDR)
        self.assertIn("Invalid TRON address", str(ctx.exception))

    def test_tron_usdt_accepts_tron_format_not_eth(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            validate_fee_collection_address("USDT", self.ETH_ADDR)
        self.assertIn("Invalid TRON address", str(ctx.exception))

    def test_malformed_tron_address_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            validate_fee_collection_address("TRX", "not-a-tron-address")
        self.assertIn("Invalid TRON address", str(ctx.exception))

    @mock.patch(
        "shkeeper.services.store_service._sidecar_managed_addresses",
        return_value={"0x" + "a" * 40},
    )
    @mock.patch(
        "shkeeper.services.store_service._known_fda_addresses",
        return_value=set(),
    )
    def test_eth_managed_address_match_ignores_case(self, _fda, _managed) -> None:
        with self.assertRaises(ValueError) as ctx:
            validate_fee_collection_address("ETH", "0x" + "A" * 40)
        self.assertIn("admin wallet", str(ctx.exception))

    @mock.patch(
        "shkeeper.services.store_service._sidecar_managed_addresses",
        return_value=set(),
    )
    @mock.patch(
        "shkeeper.services.store_service._known_fda_addresses",
        return_value=set(),
    )
    def test_tron_lowercase_is_not_treated_as_same_address(self, _fda, _managed) -> None:
        # Sidecar lists a case-folded string; a real Base58 address must not match it.
        _managed.return_value = {self.TRON_ADDR.lower()}
        self.assertEqual(
            validate_fee_collection_address("TRX", self.TRON_ADDR),
            self.TRON_ADDR,
        )

    @mock.patch(
        "shkeeper.services.store_service._sidecar_managed_addresses",
        return_value=set(),
    )
    @mock.patch(
        "shkeeper.services.store_service._known_fda_addresses",
        return_value=set(),
    )
    def test_valid_tron_address_accepted(self, _fda, _managed) -> None:
        self.assertEqual(
            validate_fee_collection_address("TRX", self.TRON_ADDR),
            self.TRON_ADDR,
        )

    @mock.patch("shkeeper.services.store_service._sidecar_managed_addresses")
    @mock.patch(
        "shkeeper.services.store_service._known_fda_addresses",
        return_value=set(),
    )
    def test_utxo_allows_generated_invoice_address(self, _fda, _managed) -> None:
        cases = {
            "BTC": "tb1qmgapuwhrr6mpukhtm4jwgsyzc6wcudt5a9w4su",
            "LTC": "ltc1qw508d6qejxtdg4y5r3zarvary0c5xw7kgmn4n9",
            "DOGE": "DFKpgdtv6KLLPuWmYkBqAnxDyXjGgBEirB",
        }
        for crypto, addr in cases.items():
            _managed.return_value = {addr}
            self.assertEqual(validate_fee_collection_address(crypto, addr), addr)
        _managed.assert_not_called()

    def test_sidecar_managed_addresses_skips_utxo_backends(self) -> None:
        from shkeeper.modules.classes.crypto import Crypto
        from shkeeper.modules.classes.utxo_like_wallet_crypto import (
            UtxoLikeWalletCrypto,
        )
        from shkeeper.services.store_service import _sidecar_managed_addresses

        saved = dict(Crypto.instances)
        try:

            class _UtxoDouble(UtxoLikeWalletCrypto):
                env_prefix = "BTC"
                default_host = "localhost"

                def __init__(self):
                    self.crypto = "BTC"

                def getname(self):
                    return "Bitcoin"

            crypto = _UtxoDouble()
            crypto.get_all_addresses = mock.Mock(
                side_effect=AssertionError("UTXO sidecar should not be queried")
            )
            Crypto.instances["BTC"] = crypto
            self.assertEqual(_sidecar_managed_addresses("BTC"), set())
            crypto.get_all_addresses.assert_not_called()
        finally:
            Crypto.instances.clear()
            Crypto.instances.update(saved)

    def test_utxo_requires_checksum_valid_address(self) -> None:
        doge = "DFKpgdtv6KLLPuWmYkBqAnxDyXjGgBEirB"
        self.assertEqual(validate_fee_collection_address("DOGE", doge), doge)
        with self.assertRaises(ValueError) as ctx:
            validate_fee_collection_address("DOGE", "any-destination")
        self.assertIn("Invalid DOGE address", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            validate_fee_collection_address("DOGE", doge[:-1] + "X")
        self.assertIn("Invalid DOGE address", str(ctx.exception))
        self.assertIsNone(validate_fee_collection_address("DOGE", "   "))


class TestAddressFormatHardening(unittest.TestCase):
    TRON_ADDR = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
    ETH_ADDR = "0x" + "ab" * 20

    def test_eth_rejects_int_permissive_forms(self) -> None:
        rejected = [
            "0x+000000000000000000000000000000000000000",
            "0x-000000000000000000000000000000000000000",
            "0x1_00000000000000000000000000000000000000",
            "0x  11111111111111111111111111111111111111",
            "0x" + "a" * 39,
            "0x" + "g" * 40,
        ]
        for address in rejected:
            self.assertFalse(_is_valid_eth_address(address), address)
            with self.assertRaises(ValueError):
                validate_fee_collection_address("ETH", address)

    def test_eth_accepts_plain_hex(self) -> None:
        self.assertTrue(_is_valid_eth_address(self.ETH_ADDR))
        self.assertTrue(_is_valid_eth_address("0X" + "AB" * 20))

    def test_tron_checksum_rejects_single_character_typo(self) -> None:
        typo = self.TRON_ADDR[:-1] + "u"
        self.assertTrue(_is_valid_tron_address(self.TRON_ADDR))
        self.assertFalse(_is_valid_tron_address(typo))
        with self.assertRaises(ValueError) as ctx:
            validate_fee_collection_address("TRX", typo)
        self.assertIn("Invalid TRON address", str(ctx.exception))

    def test_merchant_wallet_strips_and_checks_format(self) -> None:
        self.assertIsNone(validate_merchant_wallet_address("ETH", "  "))
        self.assertEqual(
            validate_merchant_wallet_address("ETH", "  " + self.ETH_ADDR + "\n"),
            self.ETH_ADDR,
        )
        with self.assertRaises(ValueError) as ctx:
            validate_merchant_wallet_address("ETH", "0x" + "a" * 39)
        self.assertIn("Invalid Ethereum merchant wallet address", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            validate_merchant_wallet_address("USDT", self.TRON_ADDR[:-1] + "u")
        self.assertIn("Invalid TRON merchant wallet address", str(ctx.exception))

    def test_merchant_wallet_accepts_utxo_destination(self) -> None:
        cases = {
            "BTC": "tb1qmgapuwhrr6mpukhtm4jwgsyzc6wcudt5a9w4su",
            "LTC": "ltc1qw508d6qejxtdg4y5r3zarvary0c5xw7kgmn4n9",
            "DOGE": "DFKpgdtv6KLLPuWmYkBqAnxDyXjGgBEirB",
        }
        for crypto, dest in cases.items():
            self.assertEqual(validate_merchant_wallet_address(crypto, dest), dest)
        with self.assertRaises(ValueError) as ctx:
            validate_merchant_wallet_address("LTC", cases["BTC"])
        self.assertIn("Invalid LTC merchant wallet address", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            validate_merchant_wallet_address("BTC", cases["DOGE"])
        self.assertIn("Invalid BTC merchant wallet address", str(ctx.exception))
        with self.assertRaises(ValueError):
            validate_merchant_wallet_address(
                "LTC", "ltc1qw508d6qejxtdg4y5r3zarvary0c5xw7kgmn4np"
            )

    def test_utxo_accepts_legacy_and_segwit_forms(self) -> None:
        accepted = {
            "BTC": [
                "1BoatSLRHtKNngkdXEeobR76b53LETtpyT",
                "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy",
                "BC1QW508D6QEJXTDG4Y5R3ZARVARY0C5XW7KV8F3T4",
                "bc1pqqqsyqcyq5rqwzqfpg9scrgwpugpzysnzs23v9ccrydpk8qarc0sg5tmnz",
            ],
            "LTC": [
                "LVuDpNCSSj6pQ7t9Pv6d6sUkLKoqDEVUnJ",
                "MJaRnao1s62a2zAKSkmG582KbLKianqb7v",
                "ltc1qw508d6qejxtdg4y5r3zarvary0c5xw7kgmn4n9",
            ],
            "DOGE": [
                "DFKpgdtv6KLLPuWmYkBqAnxDyXjGgBEirB",
                "A37YDYSwz3438rFtm1SLVcQHyD7JeueC9H",
            ],
        }
        for crypto, addresses in accepted.items():
            for address in addresses:
                self.assertTrue(_is_valid_utxo_address(address, crypto), address)
        self.assertFalse(
            _is_valid_utxo_address("Bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4", "BTC")
        )


class TestCoerceFeePercent(unittest.TestCase):
    def test_rejects_non_finite_negative_and_over_100(self) -> None:
        for raw in ("NaN", "Infinity", "-Infinity", "-50", "1E+9999", "100.00000001", "101"):
            with self.assertRaises(ValueError, msg=raw):
                _coerce_fee_percent(raw)

    def test_accepts_bounds(self) -> None:
        self.assertEqual(_coerce_fee_percent("0"), Decimal("0"))
        self.assertEqual(_coerce_fee_percent("2.5"), Decimal("2.5"))
        self.assertEqual(_coerce_fee_percent("100"), Decimal("100"))
        self.assertEqual(_coerce_fee_percent(Decimal("99.99")), Decimal("99.99"))
