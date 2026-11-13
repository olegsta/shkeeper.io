from collections import namedtuple
import datetime
from functools import cached_property
from os import environ, path, unlink
import base64, codecs, json, requests
import threading
from time import sleep, time
from decimal import Decimal
from typing import List, Literal, Tuple

from flask import current_app as app

from shkeeper.events import shkeeper_initialized
from shkeeper.models import BitcoinLightningInvoice as BLI, Setting
from shkeeper.modules.classes.crypto import Crypto
from shkeeper import db
from shkeeper.utils import format_decimal, remove_exponent
from shkeeper.wallet_encryption import wallet_encryption


class BitcoinLightning(Crypto):
    _display_name = "BTC Lightning"
    # Own network so store provisioning does not share an FDA group with on-chain BTC.
    network_currency = "BTC-LIGHTNING"

    def __init__(self) -> None:
        self.crypto = "BTC-LIGHTNING"

        self.RTL_WEB_URL = environ.get("RTL_WEB_URL", "http://127.0.0.1:3000")

        # Handle LNBITS_URL with proper validation and normalization
        lnbits_url = environ.get("LNBITS_URL")
        if lnbits_url:
            # Strip trailing slashes
            lnbits_url = lnbits_url.rstrip("/")
            # Add https:// prefix if no scheme is present
            if not lnbits_url.startswith(("http://", "https://")):
                lnbits_url = f"https://{lnbits_url}"
            self.LNBITS_URL = lnbits_url + ":9000"
        else:
            raise ValueError("LNBITS_URL is not set")

        self.LNBITS_ADMIN_PASSWORD = environ.get("LNBITS_ADMIN_PASSWORD")
        self.LNBITS_SHARED_DIR = environ.get("LNBITS_SHARED_DIR", "/lnbits_shared")

        self.LND_SHARED_DIR = environ.get("LND_SHARED_DIR", "/lightning_shared")
        self.LND_REST_URL = environ.get("LND_REST_URL", "https://lnd:8080")
        self.LND_NETWORK = environ.get(
            "LND_NETWORK",
            "mainnet",  # regtest, testnet, mainnet
        )
        self.LIGHTNING_INVOICE_TTL = int(
            environ.get("LIGHTNING_INVOICE_TTL", 60 * 60 * 24 * 7)
        )
        self.LIGHTNING_INVOICE_REFRESH_PERIOD = int(
            environ.get("LIGHTNING_INVOICE_REFRESH_PERIOD", 3)
        )
        self.LIGHTNING_INVOICE_ERROR_WAIT_PERIOD = int(
            environ.get("LIGHTNING_INVOICE_ERROR_WAIT_PERIOD", 60)
        )
        self.LIGHTNING_SEND_TO_SHKEEPER_PERIOD = int(
            environ.get("LIGHTNING_SEND_TO_SHKEEPER_PERIOD", 1)
        )

        self.LIGHTNING_REQUESTS_TIMEOUT = int(
            environ.get("LIGHTNING_REQUESTS_TIMEOUT", 60)
        )

        self.LIGHTNING_WALLET_UNLOCK_PERIOD = int(
            environ.get("LIGHTNING_WALLET_UNLOCK_PERIOD", 5)
        )

        self.LIGHTNING_WALLET_SEED_SAVER_PERIOD = int(
            environ.get("LIGHTNING_WALLET_SEED_SAVER_PERIOD", 5)
        )

        self.LIGHTNING_GENERATE_ONCHAIN_ADDRESS = bool(
            environ.get("LIGHTNING_GENERATE_ONCHAIN_ADDRESS", False)
        )

        self.LIGHTNING_AUTOPAYOUT_MAX_ATTEMPTS = int(
            environ.get("LIGHTNING_AUTOPAYOUT_MAX_ATTEMPTS", "50")
        )

        self.LIGHTNING_AUTOPAYOUT_SEND_PERCENT = Decimal(
            environ.get("LIGHTNING_AUTOPAYOUT_SEND_PERCENT", "100")
        )

        self._lnurl = None
        self._store_lnurls = {}
        self._threads_started = False

    def start_threads(self, flask_app):
        if self._threads_started:
            flask_app.logger.warning("Threads already started.")
            return
        self._threads_started = True

        threading.Thread(
            target=self.invoice_listener,
            name="invoice listener",
            daemon=True,
            args=(flask_app,),
        ).start()

        threading.Thread(
            target=self.invoice_refresher,
            name="invoice refresher",
            daemon=True,
            args=(flask_app,),
        ).start()

        threading.Thread(
            target=self.invoice_notificator,
            name="invoice notificator",
            daemon=True,
            args=(flask_app,),
        ).start()

        # threading.Thread(
        #     target=self.wallet_unlocker,
        #     name="wallet_unlocker",
        #     daemon=True,
        #     args=(flask_app,),
        # ).start()

        threading.Thread(
            target=self.seed_saver,
            name="seed saver",
            daemon=True,
            args=(flask_app,),
        ).start()

        threading.Thread(
            target=self.lnurl_setup,
            name="lnurl setup",
            daemon=True,
            args=(flask_app,),
        ).start()

    def getname(self):
        return self.crypto

    def gethost(self):
        return self.LND_REST_URL

    @cached_property
    def session(self):
        s = requests.Session()
        s.verify = self.tls_cert
        s.headers = {"Grpc-Metadata-macaroon": self.macaroon}
        return s

    @cached_property
    def macaroon(self):
        macaroon_path = path.join(
            self.LND_SHARED_DIR,
            "data/chain/bitcoin",
            self.LND_NETWORK,
            "admin.macaroon",
        )
        return codecs.encode(open(macaroon_path, "rb").read(), "hex")

    @property
    def rtl_cookie(self):
        return open(
            path.join(
                self.LND_SHARED_DIR,
                "rtl-cookie",
            ),
        ).read()

    @cached_property
    def tls_cert(self):
        return path.join(
            self.LND_SHARED_DIR,
            "tls.cert",
        )

    def getstatus(self) -> str:
        try:
            info = self.session.get(
                f"{self.LND_REST_URL}/v1/getinfo",
                timeout=self.LIGHTNING_REQUESTS_TIMEOUT,
            ).json()
            if "message" in info:
                return info["message"]
            if info["synced_to_chain"]:
                status = "Synced"
            else:
                block_ts = int(info["best_header_timestamp"])
                now_ts = int(datetime.datetime.now().timestamp())
                delta = abs(now_ts - block_ts)
                status = f"{delta} seconds lag"
            return status
        except Exception as e:
            app.logger.exception(f"Can't get status: {e}")
            return "Offline"

    @staticmethod
    def sat_to_btc(sat: Decimal):
        return sat / 100_000_000

    @staticmethod
    def msat_to_btc(msat: Decimal):
        return msat / 100_000_000_000

    @staticmethod
    def btc_to_sat(btc: Decimal):
        return btc * 100_000_000

    @staticmethod
    def btc_to_msat(btc: Decimal):
        return btc * 100_000_000_000

    @staticmethod
    def _normalize_store_id(store_id) -> int:
        if store_id is None or store_id == "":
            return 1
        store_id = int(store_id)
        if store_id <= 0:
            raise ValueError(f"Invalid store_id {store_id}")
        return store_id

    def _store_username(self, store_id: int) -> str:
        return f"shkeeper-store-{store_id}"

    def _lnbits_admin_token(self, force: bool = False) -> str:
        now = time()
        token = getattr(self, "_admin_token", None)
        if token and not force and now < getattr(self, "_admin_token_exp", 0):
            return token
        username = environ.get("LNBITS_ADMIN_USER")
        password = environ.get("LNBITS_ADMIN_PASSWORD")
        if not username or not password:
            raise RuntimeError(
                "LNBITS_ADMIN_USER or LNBITS_ADMIN_PASSWORD is not set"
            )
        response = requests.post(
            f"{self.LNBITS_URL}/api/v1/auth",
            json={"username": username, "password": password},
            timeout=self.LIGHTNING_REQUESTS_TIMEOUT,
            verify=False,
        )
        try:
            body = response.json()
        except Exception:
            body = {"detail": response.text}
        access_token = body.get("access_token") if isinstance(body, dict) else None
        if response.status_code not in (200, 201) or not access_token:
            raise RuntimeError(
                f"LNbits login failed: {self._lnbits_error(body)}"
            )
        self._admin_token = access_token
        self._admin_token_exp = now + 20 * 60
        return access_token

    def _lnbits_api(
        self, method: str, path: str, api_key: str, payload=None, account: bool = False
    ):
        headers = {"Content-Type": "application/json"}
        if account:
            headers["Authorization"] = f"Bearer {self._lnbits_admin_token()}"
        elif api_key:
            headers["X-API-KEY"] = api_key.strip()
        response = requests.request(
            method,
            f"{self.LNBITS_URL}{path}",
            headers=headers,
            json=payload,
            timeout=self.LIGHTNING_REQUESTS_TIMEOUT,
            verify=False,
        )
        if account and response.status_code == 401:
            headers["Authorization"] = f"Bearer {self._lnbits_admin_token(force=True)}"
            response = requests.request(
                method,
                f"{self.LNBITS_URL}{path}",
                headers=headers,
                json=payload,
                timeout=self.LIGHTNING_REQUESTS_TIMEOUT,
                verify=False,
            )
        try:
            body = response.json()
        except Exception:
            body = {"detail": response.text}
        return response.status_code, body

    @staticmethod
    def _lnbits_error(body) -> str:
        if isinstance(body, dict):
            detail = body.get("detail") or body.get("message") or body.get("error")
            if isinstance(detail, list):
                return "; ".join(str(item) for item in detail)
            if detail:
                return str(detail)
        return str(body)

    @staticmethod
    def _wallet_keys(wallet: dict):
        admin_key = wallet.get("adminkey") or wallet.get("admin_key")
        invoice_key = wallet.get("inkey") or wallet.get("invoice_key")
        wallet_id = wallet.get("id") or wallet.get("wallet_id")
        if not admin_key or not invoice_key or not wallet_id:
            return None
        return wallet_id, admin_key, invoice_key

    def _load_store_wallet(self, store_id: int):
        from shkeeper.models import BitcoinLightningStoreWallet

        return BitcoinLightningStoreWallet.query.get(store_id)

    def _account_wallets(self, name: str):
        status, body = self._lnbits_api(
            "GET",
            f"/api/v1/wallet/paginated?search={name}&limit=50",
            "",
            account=True,
        )
        if status >= 300:
            raise RuntimeError(
                f"LNbits wallet list failed: {self._lnbits_error(body)}"
            )
        rows = body.get("data") if isinstance(body, dict) else body
        return [
            row
            for row in rows or []
            if row.get("name") == name and self._wallet_keys(row) and row.get("user")
        ]

    def _create_lnbits_wallet(self, store_id: int) -> dict:
        """Create a wallet on the logged-in LNbits account. Returns keys, no DB write."""
        name = self._store_username(store_id)
        existing = self._account_wallets(name)
        wallet = existing[0] if existing else None
        if wallet is None:
            status, wallet = self._lnbits_api(
                "POST",
                "/api/v1/wallet",
                "",
                {"name": name},
                account=True,
            )
            if (
                status not in (200, 201)
                or not self._wallet_keys(wallet or {})
                or not (wallet or {}).get("user")
            ):
                raise RuntimeError(
                    f"LNbits wallet create failed for store {store_id}: "
                    f"{self._lnbits_error(wallet)}"
                )

        wallet_id, admin_key, invoice_key = self._wallet_keys(wallet)
        return {
            "user_id": wallet["user"],
            "wallet_id": wallet_id,
            "admin_key": admin_key,
            "invoice_key": invoice_key,
        }

    def prepare_store(self, store_id=None):
        """Return the LNbits wallet row for this store, creating it if needed."""
        from shkeeper.models import BitcoinLightningStoreWallet

        store_id = self._normalize_store_id(store_id)
        existing = self._load_store_wallet(store_id)
        if existing and existing.admin_key and existing.invoice_key:
            return existing
        created = self._create_lnbits_wallet(store_id)
        row = existing or BitcoinLightningStoreWallet(store_id=store_id)
        row.user_id = created["user_id"]
        row.wallet_id = created["wallet_id"]
        row.admin_key = created["admin_key"]
        row.invoice_key = created["invoice_key"]
        db.session.add(row)
        db.session.commit()
        return row

    def balance(self, store_id=None) -> Decimal:
        return self.balance_for_account(store_id=store_id)

    def balance_for_account(self, store_id=None) -> Decimal:
        try:
            wallet = self.prepare_store(store_id)
            status, body = self._lnbits_api(
                "GET", "/api/v1/wallet", wallet.invoice_key
            )
            if status >= 300:
                app.logger.warning(
                    "LNbits balance failed store_id=%s: %s",
                    store_id,
                    self._lnbits_error(body),
                )
                return Decimal(0)
            return self.msat_to_btc(Decimal(body.get("balance") or 0))
        except Exception as exc:
            app.logger.exception("LNbits balance failed: %s", exc)
            return Decimal(0)

    def query_routes(self, pub_key: str, amt_sat: int) -> bool:
        """Ask LND (QueryRoutes) whether a route exists to pub_key for amt_sat satoshis."""
        app.logger.debug(
            f"query_routes: checking route to pub_key={pub_key} for amt_sat={amt_sat}"
        )
        try:
            resp = self.session.get(
                f"{self.LND_REST_URL}/v1/graph/routes/{pub_key}/{amt_sat}",
                timeout=self.LIGHTNING_REQUESTS_TIMEOUT,
            )
            r = resp.json()
            app.logger.debug(
                f"query_routes: response status_code={resp.status_code} body={r!r}"
            )
            if resp.status_code != 200:
                app.logger.debug(
                    f"query_routes: no route for amt_sat={amt_sat}: {r.get('message', r)!r}"
                )
                return False
            routes = r.get("routes") or []
            found = len(routes) > 0
            app.logger.debug(
                f"query_routes: {'found' if found else 'no'} route(s) for amt_sat={amt_sat} ({len(routes)} routes)"
            )
            return found
        except Exception as e:
            app.logger.exception(
                f"query_routes: request failed for pub_key={pub_key} amt_sat={amt_sat}: {e}"
            )
            return False

    def find_max_payable_amount(
        self, pub_key: str, high_sat: int, floor_sat: int = 1000
    ) -> int:
        """Binary search the max amount (in sats) payable to pub_key, between floor_sat and high_sat."""
        max_attempts = self.LIGHTNING_AUTOPAYOUT_MAX_ATTEMPTS
        app.logger.debug(
            f"find_max_payable_amount: searching pub_key={pub_key} between "
            f"floor_sat={floor_sat} and high_sat={high_sat}, max_attempts={max_attempts}"
        )
        if high_sat < floor_sat:
            app.logger.debug(
                f"find_max_payable_amount: high_sat={high_sat} is below floor_sat={floor_sat}, nothing payable"
            )
            return 0

        attempts = 1
        if self.query_routes(pub_key, high_sat):
            app.logger.debug(
                f"find_max_payable_amount: full high_sat={high_sat} is payable"
            )
            return high_sat

        low, high, best = floor_sat, high_sat - 1, 0
        while low <= high:
            if attempts >= max_attempts:
                app.logger.warning(
                    f"find_max_payable_amount: reached max_attempts={max_attempts}, stopping search early, best so far={best} sats"
                )
                break
            attempts += 1
            mid = (low + high) // 2
            if self.query_routes(pub_key, mid):
                app.logger.debug(
                    f"find_max_payable_amount: mid={mid} sats payable, trying higher (low={low}, high={high})"
                )
                best = mid
                low = mid + 1
            else:
                app.logger.debug(
                    f"find_max_payable_amount: mid={mid} sats not payable, trying lower (low={low}, high={high})"
                )
                high = mid - 1

        app.logger.debug(
            f"find_max_payable_amount: best payable amount={best} sats after {attempts} attempt(s)"
        )
        return best

    def _lookup_dest_pubkey(self, destination: str, hint_amount: Decimal) -> str:
        """Resolve a Lightning destination (LNURL/lightning-address/payment request) to the receiver's node pubkey."""
        app.logger.debug(
            f"_lookup_dest_pubkey: resolving destination={destination} hint_amount={hint_amount}"
        )
        pay_req, lnurl_info = self.lnurl_to_pr(
            destination, remove_exponent(self.btc_to_msat(hint_amount))
        )
        app.logger.debug(f"_lookup_dest_pubkey: resolved pay_req={pay_req}")

        decoded_pay_req = self.session.get(
            f"{self.LND_REST_URL}/v1/payreq/{pay_req}",
            timeout=self.LIGHTNING_REQUESTS_TIMEOUT,
        ).json()
        app.logger.debug(f"_lookup_dest_pubkey: decoded payreq={decoded_pay_req!r}")

        if "destination" not in decoded_pay_req:
            raise Exception(
                decoded_pay_req.get(
                    "message", f"Unable to decode payment request: {decoded_pay_req!r}"
                )
            )

        pub_key = decoded_pay_req["destination"]
        app.logger.debug(f"_lookup_dest_pubkey: resolved pub_key={pub_key}")
        return pub_key

    def find_sendable_amount(
        self, destination: str, high_amount: Decimal, floor_sat: int = 1000
    ) -> Decimal:
        """
        Discover the actual sendable amount (in BTC) to destination using LND QueryRoutes.

        Binary searches for the maximum amount (up to high_amount) for which a route
        exists, then returns LIGHTNING_AUTOPAYOUT_SEND_PERCENT% of that discovered
        maximum as a safety margin. Returns Decimal(0) if no route can be found or
        destination resolution fails.
        """
        send_percent = self.LIGHTNING_AUTOPAYOUT_SEND_PERCENT
        app.logger.info(
            f"find_sendable_amount: destination={destination} high_amount={high_amount} "
            f"floor_sat={floor_sat} send_percent={send_percent}"
        )
        try:
            pub_key = self._lookup_dest_pubkey(destination, high_amount)
        except Exception as e:
            app.logger.exception(
                f"find_sendable_amount: failed to resolve destination pubkey for {destination}: {e}"
            )
            return Decimal(0)

        high_sat = int(self.btc_to_sat(high_amount))
        app.logger.debug(
            f"find_sendable_amount: searching for max payable amount up to high_sat={high_sat} to pub_key={pub_key}"
        )

        max_payable_sat = self.find_max_payable_amount(pub_key, high_sat, floor_sat)
        if max_payable_sat <= 0:
            app.logger.warning(
                f"find_sendable_amount: no route found to pub_key={pub_key} up to high_sat={high_sat}"
            )
            return Decimal(0)

        sendable_sat = Decimal(max_payable_sat) * (send_percent / Decimal(100))
        sendable_btc = self.sat_to_btc(sendable_sat)
        app.logger.info(
            f"find_sendable_amount: max_payable_sat={max_payable_sat}, "
            f"sending {send_percent}% = sendable_sat={sendable_sat} ({sendable_btc} BTC)"
        )
        return sendable_btc

    @staticmethod
    def to_hex_string(b64_string):
        return codecs.encode(base64.b64decode(b64_string), "hex").decode()

    @staticmethod
    def to_base64_string(hex_string):
        return base64.b64encode(codecs.decode(hex_string, "hex")).decode()

    def _upsert_invoice(self, payload, store_id):
        store_id = self._normalize_store_id(store_id)
        existing = BLI.query.filter_by(r_hash=payload["r_hash"]).first()
        if existing:
            if existing.store_id is None:
                existing.store_id = store_id
            existing.update(**payload)
            return existing
        bli = BLI()
        bli.store_id = store_id
        bli.update(**payload)
        return bli

    def mkaddr(self, **kwargs) -> str:
        store_id = self._normalize_store_id(kwargs.get("store_id"))
        wallet = self.prepare_store(store_id)
        details = dict(kwargs.get("details") or {})
        amount_btc = Decimal(str(details.get("value") or 0))
        amount_sat = int(self.btc_to_sat(amount_btc))
        payload = {
            "out": False,
            "amount": amount_sat,
            "expiry": self.LIGHTNING_INVOICE_TTL,
        }
        if details.get("memo"):
            payload["memo"] = details["memo"]
        status, body = self._lnbits_api(
            "POST", "/api/v1/payments", wallet.invoice_key, payload
        )
        if status not in (200, 201) or not body.get("payment_request"):
            raise RuntimeError(
                f"Failed to generate address: {self._lnbits_error(body)}"
            )
        self._upsert_invoice(
            {
                "r_hash": body.get("payment_hash"),
                "payment_request": body["payment_request"],
                "value": amount_sat,
            },
            store_id,
        )
        return body["payment_request"]

    def invoice_listener(self, app):
        app.logger.debug("Thread started.")

        while True:
            with app.app_context():
                try:
                    r = self.session.get(
                        f"{self.LND_REST_URL}/v1/invoices/subscribe",
                        stream=True,
                        timeout=None,
                    )
                    if r.status_code > 200:
                        raise Exception(f"Invoice listener subscribing error: {r.text}")
                    for raw_response in r.iter_lines():
                        json_response = json.loads(raw_response)
                        app.logger.debug(f"invoice update: {json_response}")
                        r = json_response["result"]
                        r["r_hash"] = self.to_hex_string(r["r_hash"])
                        if inv := BLI.query.filter(BLI.r_hash == r["r_hash"]).first():
                            inv.update(**r)
                        else:
                            app.logger.debug(
                                f"invoice not (yet) found in db. Update body: {json_response}"
                            )
                except Exception as e:
                    app.logger.exception(f"error: {e}")
                    sleep(self.LIGHTNING_INVOICE_ERROR_WAIT_PERIOD)

    def invoice_refresher(self, app):
        app.logger.debug("Thread started.")

        inactive_statuses = ("SETTLED", "CANCELED")
        while True:
            sleep(self.LIGHTNING_INVOICE_REFRESH_PERIOD)
            with app.app_context():
                try:
                    unpaid_invoices = BLI.query.filter(
                        (BLI.state.not_in(inactive_statuses)) | BLI.state.is_(None)
                    ).all()
                    if len(unpaid_invoices):
                        app.logger.debug(
                            f"db has {len(unpaid_invoices)} unpaid invoces"
                        )
                    for inv in unpaid_invoices:
                        r = self.session.get(
                            f"{self.LND_REST_URL}/v1/invoice/{inv.r_hash}",
                            timeout=self.LIGHTNING_REQUESTS_TIMEOUT,
                        ).json()
                        r["r_hash"] = self.to_hex_string(r["r_hash"])
                        inv.update(**r)
                except Exception as e:
                    app.logger.exception(f"error: {e}")

    def invoice_notificator(self, app):
        app.logger.debug("Thread started.")

        s = requests.Session()
        s.headers = {
            "X-Shkeeper-Backend-Key": environ.get(
                "SHKEEPER_BTC_BACKEND_KEY", "shkeeper"
            )
        }
        while True:
            sleep(self.LIGHTNING_SEND_TO_SHKEEPER_PERIOD)
            with app.app_context():
                try:
                    paid_invoices = BLI.query.filter(
                        (BLI.state == "SETTLED") & (BLI.sent_to_shkeeper == False)
                    ).all()
                    if len(paid_invoices):
                        app.logger.debug(f"{len(paid_invoices)} notifications pending")
                    for inv in paid_invoices:
                        app.logger.debug(f"sending notification for {inv.r_hash}")
                        r = s.post(
                            f"http://localhost:5000/api/v1/walletnotify/{self.crypto}/{inv.r_hash}",
                            timeout=300,
                        )
                        app.logger.debug(f"{inv.r_hash} response: {r.text}")
                        r = r.json()
                        if "success" == r["status"]:
                            inv.update(sent_to_shkeeper=True)

                except Exception as e:
                    app.logger.exception(f"error: {e}")

    def wallet_unlocker(self, app):
        app.logger.debug("Thread started.")

        while True:
            with app.app_context():
                try:
                    key = wallet_encryption.wait_for_key()
                    wallet_password = base64.b64encode(key.zfill(8).encode()).decode()

                    unlockwallet_response = self.session.post(
                        f"{self.LND_REST_URL}/v1/unlockwallet",
                        data=json.dumps(
                            {
                                "wallet_password": wallet_password,
                            }
                        ),
                        timeout=self.LIGHTNING_REQUESTS_TIMEOUT,
                    )
                    r = unlockwallet_response.json()

                    if unlockwallet_response.status_code > 200:
                        if "wallet already unlocked" not in r["message"]:
                            app.logger.debug(f"unlockwallet api error: {r['message']}")
                    else:
                        app.logger.info("wallet has been unlocked!")

                except Exception as e:
                    app.logger.exception(f"error: {e}")

            sleep(self.LIGHTNING_WALLET_UNLOCK_PERIOD)

    def seed_saver(self, app):
        app.logger.debug("Thread started.")

        while True:
            sleep(self.LIGHTNING_WALLET_SEED_SAVER_PERIOD)
            with app.app_context():
                try:
                    setting_name = "btc_lightning_wallet_seed"
                    setting = Setting.query.get(setting_name)
                    if setting:
                        app.logger.debug("seed is already saved to db")
                        break

                    seed_path = path.join(
                        self.LND_SHARED_DIR,
                        "wallet-seed",
                    )
                    app.logger.debug(f"Reading seed from {seed_path}...")
                    cipher_seed_mnemonic = open(seed_path, "rb").read().decode()

                    app.logger.debug(f"Encrypting seed...")
                    encrypted_seed = wallet_encryption.encrypt_text(
                        cipher_seed_mnemonic
                    )

                    app.logger.debug(f"Saving enctypted seed to db...")
                    db.session.add(Setting(name=setting_name, value=encrypted_seed))
                    db.session.commit()
                    app.logger.debug(f"seed saved.")

                    app.logger.debug(f"removing seed file {seed_path}...")
                    unlink(seed_path)
                    break

                except Exception as e:
                    app.logger.exception(f"error: {e}")

        app.logger.debug(f"thread exiting.")

    def lnurl_setup(self, app):
        app.logger.debug("Thread started.")

        sleep(5)  # Wait for LNbits to be ready

        with app.app_context():
            while True:
                try:
                    lnurl = self.get_lnurl(store_id=1)
                    if not lnurl:
                        sleep(30)
                        continue
                    self._lnurl = lnurl
                    app.logger.debug(f"LNURL ready: {self._lnurl}")
                    break
                except Exception as e:
                    app.logger.exception(f"error: {e}")
                    sleep(30)

        app.logger.debug(f"thread exiting.")

    def getaddrbytx(
        self, txid
    ) -> List[Tuple[str, Decimal, int, Literal["send", "receive"]]]:
        inv: BLI = BLI.query.filter(BLI.r_hash == txid).first()
        confirmations = 999
        address = inv.payment_request
        amount = self.sat_to_btc(inv.value)
        operation = "receive"
        info = (address, amount, confirmations, operation)
        return [info]

    def get_confirmations_by_txid(self, txid, store_id=None) -> int:
        return 999

    def estimate_tx_fee(self, amount, **kwargs):
        pay_req, lnurl_info = self.lnurl_to_pr(kwargs["address"])
        app.logger.debug(f"Estimated TX fee for {pay_req}:")
        decoded_pay_req = self.session.get(
            f"{self.LND_REST_URL}/v1/payreq/{pay_req}",
            timeout=self.LIGHTNING_REQUESTS_TIMEOUT,
        ).json()
        app.logger.debug(f"{decoded_pay_req!r}")
        if "destination" not in decoded_pay_req:
            return {
                "status": "error",
                "error": (
                    decoded_pay_req["message"]
                    if "message" in decoded_pay_req
                    else f"{decoded_pay_req!r}"
                ),
            }

        expiration_ts = int(decoded_pay_req["timestamp"]) + int(
            decoded_pay_req["expiry"]
        )
        exp_date = datetime.datetime.fromtimestamp(expiration_ts).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        if expiration_ts <= time():
            return {
                "status": "error",
                "error": f"The invoice has been expired. Expiration date: {exp_date}",
            }

        return {
            "status": "success",
            "fee": 0,
            # "fee_estimate_details": fee_estimate,
            "payment_request_details": decoded_pay_req,
            "lnurl_info": lnurl_info,
        }

    def mkpayout(
        self,
        destination: str,
        amount: Decimal,
        fee: int,
        subtract_fee_from_amount: bool = False,
        store_id=None,
    ):
        amount = Decimal(amount)
        try:
            wallet = self.prepare_store(store_id)
        except Exception as exc:
            return {"result": None, "error": {"message": str(exc)}}
        return self._send_payment(destination, amount, wallet)

    def _send_payment(self, destination: str, amount: Decimal, wallet):
        try:
            destination, lnurl_info = self.lnurl_to_pr(
                destination, remove_exponent(self.btc_to_msat(amount))
            )

            decoded_pay_req = self.session.get(
                f"{self.LND_REST_URL}/v1/payreq/{destination}",
                timeout=self.LIGHTNING_REQUESTS_TIMEOUT,
            ).json()
            app.logger.debug(f"Decoded payment request: {decoded_pay_req!r}")
            if "destination" not in decoded_pay_req:
                message = decoded_pay_req.get("message") or str(decoded_pay_req)
                return {"result": None, "error": {"message": message}}

            pay_body = {"out": True, "bolt11": destination}
            if int(decoded_pay_req.get("num_satoshis") or 0) == 0:
                pay_body["amount"] = int(self.btc_to_sat(amount))

            status, result = self._lnbits_api(
                "POST", "/api/v1/payments", wallet.admin_key, pay_body
            )
            app.logger.info(f"Payout result: {status} {result!r}")

            if status not in (200, 201):
                return {
                    "result": None,
                    "error": {"message": self._lnbits_error(result)},
                }

            payment_hash = result.get("payment_hash") or result.get("checking_id")
            if payment_hash:
                return {"result": payment_hash, "error": None}
            return {
                "result": None,
                "error": {"message": f"Unknown result: {result!r}"},
            }

        except Exception as e:
            app.logger.exception("")
            return {"result": None, "error": {"message": str(e)}}

    def multipayout(self, payout_list, store_id=None):
        """Pay each destination in order. Lightning has no single multi-output tx."""
        if not payout_list:
            return {
                "result": None,
                "error": {"message": "Expected an array of payouts"},
                "results": [],
            }
        normalized = []
        total = Decimal(0)
        for item in payout_list:
            dest = item.get("dest") or item.get("destination")
            amount = Decimal(str(item["amount"]))
            total += amount
            normalized.append((dest, amount))
        available = self.balance_for_account(store_id=store_id)
        if total > available:
            return {
                "result": None,
                "error": {
                    "message": (
                        f"Not enough {self.crypto} for this payout. "
                        f"Need {total}, balance {available}"
                    )
                },
                "results": [],
            }

        results = []
        hashes = []
        for dest, amount in normalized:
            res = self.mkpayout(dest, amount, 0, store_id=store_id)
            entry = {
                "dest": dest,
                "amount": str(amount),
                "result": res.get("result") if isinstance(res, dict) else None,
                "error": None if isinstance(res, dict) else {"message": str(res)},
            }
            if isinstance(res, dict):
                entry["error"] = res.get("error")
            results.append(entry)
            if entry["error"]:
                return {
                    "result": hashes or None,
                    "error": entry["error"],
                    "results": results,
                }
            if entry["result"]:
                hashes.append(entry["result"])
        if len(results) == 1:
            return {"result": results[0]["result"], "error": None, "results": results}
        return {"result": hashes, "error": None, "results": results}

    def create_wallet(self):
        # moved to lndinit
        return {"error": None}

    def dump_wallet(self, store_id=None) -> Tuple[str, str]:
        # The node seed spends every store. Only the default store backup includes it.
        if store_id is not None and self._normalize_store_id(store_id) != 1:
            sid = self._normalize_store_id(store_id)
            payload = {
                "store_id": sid,
                "crypto": self.crypto,
                "invoices": self.get_all_addresses(store_id=sid),
            }
            return (
                f"shkeeper-btc-lightning-store-{sid}.json",
                json.dumps(payload, indent=2),
            )
        setting_name = "btc_lightning_wallet_seed"
        setting = Setting.query.get(setting_name)
        content = wallet_encryption.decrypt_text(setting.value)
        filename = "shkeeper-btc-lightning-mnemonic-key.txt"
        return filename, content

    @property
    def wallet(self):
        return self._wallet.query.filter_by(crypto=self.crypto).first()

    def get_all_addresses(self, store_id=None) -> List[str]:
        store_id = self._normalize_store_id(store_id)
        query = BLI.query
        if store_id == 1:
            query = query.filter((BLI.store_id == 1) | BLI.store_id.is_(None))
        else:
            query = query.filter(BLI.store_id == store_id)
        return [
            invoice.payment_request
            for invoice in query.all()
            if invoice.payment_request
        ]

    #
    # LNURL
    #

    @property
    def lnbits_admin_apikey(self):
        return open(
            path.join(
                self.LNBITS_SHARED_DIR,
                "data/.lnbits_admin_key",
            ),
        ).read()

    @cached_property
    def lnbits_session(self):
        s = requests.Session()
        s.verify = False
        s.headers = {"X-API-KEY": self.lnbits_admin_apikey}
        return s

    def lnbits_decode_lnurl(self, lnurl):
        app.logger.debug(f"lnbits_decode_lnurl called with lnurl={lnurl}")
        app.logger.debug(f"Posting to {self.LNBITS_URL}/api/v1/lnurlscan")
        result = self.lnbits_session.post(
            f"{self.LNBITS_URL}/api/v1/lnurlscan", json={"lnurl": lnurl}
        ).json()
        app.logger.debug(f"lnbits_decode_lnurl result: {result}")
        return result

    def lnurl_to_pr(self, destination, amount=None):
        app.logger.debug(
            f"lnurl_to_pr called with destination={destination}, amount={amount}"
        )
        try:
            app.logger.debug(f"Decoding LNURL: {destination}")
            lnurl_info = self.lnbits_decode_lnurl(destination)
            app.logger.debug(f"LNURL decoded: {lnurl_info}")

            if amount is None:
                amount = lnurl_info["minSendable"]

            callback_url = f"{lnurl_info['callback']}?amount={amount}"
            app.logger.debug(f"Requesting payment request from: {callback_url}")
            lnurl_pr_info = requests.get(callback_url).json()
            app.logger.debug(f"Payment request response: {lnurl_pr_info}")

            new_destination = lnurl_pr_info["pr"]
            app.logger.debug(
                f"Successfully converted LNURL to payment request: {new_destination}"
            )
            return new_destination, lnurl_info
        except Exception as e:
            app.logger.debug(
                f"Failed to convert LNURL, using original destination. Error: {e}"
            )
            return destination, None

    def _request_store_id(self) -> int:
        try:
            from flask import g, has_request_context

            if has_request_context():
                store = getattr(g, "current_store", None)
                if store is not None and getattr(store, "id", None):
                    return int(store.id)
        except Exception:
            pass
        return 1

    def _ensure_store_lnurl(self, store_id: int):
        wallet = self.prepare_store(store_id)
        if wallet.lnurl:
            self._store_lnurls[store_id] = wallet.lnurl
            return wallet.lnurl
        cached = self._store_lnurls.get(store_id)
        if cached:
            return cached
        description = "ShKeeper BTC Lightning Wallet"
        try:
            status, links = self._lnbits_api(
                "GET", "/lnurlp/api/v1/links", wallet.admin_key
            )
            if status == 200:
                for link in links or []:
                    if link.get("description") == description and link.get("lnurl"):
                        wallet.lnurl = link["lnurl"]
                        db.session.add(wallet)
                        db.session.commit()
                        self._store_lnurls[store_id] = wallet.lnurl
                        return wallet.lnurl
            status, created = self._lnbits_api(
                "POST",
                "/lnurlp/api/v1/links",
                wallet.admin_key,
                {
                    "description": description,
                    "min": 1,
                    "max": 100_000_000,
                    "comment_chars": 255,
                },
            )
            if status != 201:
                app.logger.error(
                    "Failed to create store LNURL store_id=%s: %s",
                    store_id,
                    self._lnbits_error(created),
                )
                return None
            lnurl = created.get("lnurl")
            if lnurl:
                wallet.lnurl = lnurl
                db.session.add(wallet)
                db.session.commit()
                self._store_lnurls[store_id] = lnurl
            return lnurl
        except Exception as exc:
            app.logger.exception(
                "store LNURL setup failed store_id=%s: %s", store_id, exc
            )
            return None

    def get_lnurl(self, store_id=None):
        if store_id is None:
            store_id = self._request_store_id()
        store_id = self._normalize_store_id(store_id)
        cached = self._store_lnurls.get(store_id)
        if cached:
            return cached
        return self._ensure_store_lnurl(store_id)

    @property
    def fee_deposit_account(self):
        return self.fee_deposit_account_for()

    def fee_deposit_account_for(self, store_id=None):
        FeeDepositAccount = namedtuple("FeeDepositAccount", "addr balance")
        return FeeDepositAccount(
            self.get_lnurl(store_id=store_id),
            self.balance(store_id=store_id),
        )
