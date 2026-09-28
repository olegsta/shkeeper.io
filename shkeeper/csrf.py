"""Global CSRF protection for browser session requests.

Machine clients authenticate with a header or HTTP Basic. A cross-origin
form cannot attach either, so those requests are not CSRF-reachable.
payout and multipayout are not exempt as views: the admin UI calls them
with the session cookie alone, and that path must still present a token.
"""

from flask import request
from flask_wtf.csrf import CSRFProtect

# Header-authenticated only. Browser session routes (payout, multipayout,
# autopayout, payout_destinations) stay protected.
_M2M_VIEWS = (
    "payment_request",
    "get_crypto_quote",
    "walletnotify",
    "payoutnotify",
    "decryption_key",
    "test_callback_receiver",
)


class ShkeeperCSRFProtect(CSRFProtect):
    def protect(self):
        if request.headers.get("X-Shkeeper-Api-Key") or request.headers.get(
            "X-Shkeeper-Backend-Key"
        ):
            return
        if request.authorization:
            return
        super().protect()


csrf = ShkeeperCSRFProtect()


def init_csrf(app):
    # Referrer-Policy is no-referrer, so Flask-WTF's HTTPS referrer check
    # would reject every legitimate POST.
    app.config["WTF_CSRF_SSL_STRICT"] = False
    # Token lives with the session. The default one-hour limit expires a
    # payout page that stays open.
    app.config["WTF_CSRF_TIME_LIMIT"] = None
    csrf.init_app(app)

    from shkeeper import api_v1

    for name in _M2M_VIEWS:
        csrf.exempt(getattr(api_v1, name))
