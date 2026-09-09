from __future__ import annotations

import unittest
from unittest import mock

from shkeeper.services.store_service import (
    validate_fee_collection_address,
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
