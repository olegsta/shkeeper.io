from __future__ import annotations

import unittest
from decimal import Decimal
from unittest import mock

from shkeeper.modules.classes.shkeeper_wallet_crypto import UtxoLikeWalletCrypto


class TestUtxoGetConfirmations(unittest.TestCase):
    def test_raises_when_getaddrbytx_returns_empty(self) -> None:
        crypto = mock.Mock()
        crypto.getaddrbytx.return_value = []

        with self.assertRaisesRegex(RuntimeError, "No transaction details"):
            UtxoLikeWalletCrypto.get_confirmations_by_txid(crypto, "abc")

    def test_returns_confirmations_from_first_detail(self) -> None:
        crypto = mock.Mock()
        crypto.getaddrbytx.return_value = [
            ["bc1qdest", Decimal("0.455"), 2, "send"]
        ]

        self.assertEqual(
            UtxoLikeWalletCrypto.get_confirmations_by_txid(crypto, "abc"),
            2,
        )
