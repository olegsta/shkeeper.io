import functools
import os
import base64
import io
from datetime import datetime, timedelta

import pyotp
import segno
from flask import Blueprint
from flask_limiter.util import get_remote_address
# from flask_smorest import Blueprint as SmorestBlueprint
from flask import flash
from flask import g
from flask import redirect
from flask import render_template
from flask import request
from flask import session
from flask import url_for
from flask import current_app as app
from werkzeug.security import check_password_hash
from werkzeug.security import generate_password_hash

from shkeeper.models import User, Wallet, UserRole, Store, StoreStatus
from shkeeper import db, limiter
from shkeeper.services.store_service import resolve_store_by_api_key
from shkeeper.services.tenancy import is_admin_user
from shkeeper.wallet_encryption import ensure_wallet_unlocked


bp = Blueprint("auth", __name__, url_prefix="/")
# bp_auth = SmorestBlueprint("auth", __name__, url_prefix="/")

@bp.context_processor
def inject_theme():
    return {"theme": request.cookies.get("theme", "light")}


def metrics_basic_auth(view):
    @functools.wraps(view)
    def wrapped_view(**kwargs):
        metrics_username = os.environ.get("METRICS_USERNAME", "shkeeper")
        metrics_password = os.environ.get("METRICS_PASSWORD", "shkeeper")
        auth = request.authorization
        if not (
            auth
            and auth.username == metrics_username
            and auth.password == metrics_password
        ):
            return {"status": "error", "msg": "authorization requred"}, 401
        return view(**kwargs)

    return wrapped_view


def bind_user_store(user) -> bool:
    """Set g.user and g.current_store. False if the user's store is not ACTIVE."""
    if user.store_id:
        store = Store.query.get(user.store_id)
        if not store or store.status != StoreStatus.ACTIVE:
            g.user = None
            g.current_store = None
            return False
        g.user = user
        g.current_store = store
        return True
    g.user = user
    if is_admin_user(user):
        from shkeeper.services.store_service import ensure_default_store

        g.current_store = ensure_default_store()
    else:
        g.current_store = None
    return True


def basic_auth_optional(view):
    """View decorator that allow to authenticate using HTTP Basic Auth."""

    @functools.wraps(view)
    def wrapped_view(**kwargs):
        if g.user is None:
            auth = request.authorization
            if auth:
                user = User.query.filter_by(username=auth.username).first()
                if user and user.verify_password(auth.password):
                    if not bind_user_store(user):
                        return {
                            "status": "error",
                            "message": "Store is not active",
                        }, 403
                else:
                    return {
                        "status": "error",
                        "message": "Bad HTTP Basic Auth credentials",
                    }
        return view(**kwargs)

    return wrapped_view


def login_required(view):
    """View decorator that redirects anonymous users to the login page."""

    @functools.wraps(view)
    def wrapped_view(**kwargs):
        if g.user is None:
            if "X-Shkeeper-Api-Key" in request.headers:
                return {
                    "status": "error",
                    "message": "This endpoint doesn't accept X-Shkeeper-Api-Key auth",
                }
            return redirect(url_for("auth.login"))

        return view(**kwargs)

    return wrapped_view


_MAX_2FA_ATTEMPTS = 5
_2FA_LOCKOUT = timedelta(minutes=15)
_2FA_LOCK_SECONDS = int(_2FA_LOCKOUT.total_seconds())


def _twofa_attempts_key(user_id) -> str:
    return f"shkeeper-2fa-attempts:{user_id}"


def _twofa_lock_key(user_id) -> str:
    return f"shkeeper-2fa-lock:{user_id}"


def _twofa_lock_active(user) -> bool:
    """True while this account is locked out of the second factor.

    The counter lives in the rate-limit store (Redis when
    RATELIMIT_STORAGE_URI points there). login() clears the session after a
    correct password, so a session counter would let an attacker re-authenticate
    and refresh the pending-2FA window.
    """
    return limiter.storage.get(_twofa_lock_key(user.id)) > 0


def _register_twofa_failure(user) -> bool:
    # INCR is atomic in both the in-memory store and Redis, so parallel
    # guesses cannot share one counter value. Expiry is set on the first hit.
    storage = limiter.storage
    attempts = storage.incr(_twofa_attempts_key(user.id), _2FA_LOCK_SECONDS)
    app.logger.warning(
        "2FA failure %s/%s for user id=%s from %s",
        attempts,
        _MAX_2FA_ATTEMPTS,
        user.id,
        get_remote_address(),
    )
    if attempts < _MAX_2FA_ATTEMPTS:
        return False
    lock_key = _twofa_lock_key(user.id)
    storage.clear(lock_key)
    storage.incr(lock_key, _2FA_LOCK_SECONDS)
    storage.clear(_twofa_attempts_key(user.id))
    return True


def _clear_twofa_failures(user):
    storage = limiter.storage
    storage.clear(_twofa_attempts_key(user.id))
    storage.clear(_twofa_lock_key(user.id))


def _login_rate_key():
    return f"{get_remote_address()}:{request.form.get('name', '')}"


def api_key_required(view):
    @functools.wraps(view)
    def wrapped_view(**kwargs):
        if "X-Shkeeper-Api-Key" not in request.headers:
            return {"status": "error", "message": "No API key"}

        apikey = request.headers["X-Shkeeper-Api-Key"]
        store = resolve_store_by_api_key(apikey)
        if store:
            g.current_store = store
            if not store.is_default and not ensure_wallet_unlocked():
                return {
                    "status": "error",
                    "message": (
                        "Wallet encryption is locked. Ask the administrator to open "
                        "/unlock once. Until then stores cannot create addresses."
                    ),
                }, 503
            return view(**kwargs)
        return {"status": "error", "message": "Bad API key"}

    # Apply after wraps() so the limit is keyed to the real view, not the
    # shared wrapper. Explicit limits replace the 600/hour default.
    return limiter.limit("120 per minute")(wrapped_view)


@bp.before_app_request
def load_logged_in_user():
    """If a user id is stored in the session, load the user object into ``g.user``."""
    user_id = session.get("user_id")

    if user_id is None:
        g.user = None
        g.current_store = None
    else:
        user = User.query.get(user_id)
        if user.passhash:
            if not bind_user_store(user):
                session.clear()
                return
        else:
            session.clear()
            g.user = None
            g.current_store = None


# @bp.before_app_request
# def load_api_user():
#     if "X-Shkeeper-Api-Key" in request.headers:
#         apikey = request.headers["X-Shkeeper-Api-Key"]

#         if request.path in [str(path) for path in app.url_map._rules_by_endpoint['api_v1.list_crypto']]:
#             wallet = Wallet.query.filter_by(apikey=apikey).first()
#             if wallet:
#                 g.user = User.query.get(1)

#         elif request.path.startswith("/api/v1/"):
#             crypto = request.path.split('/')[3]
#             wallet = Wallet.query.filter_by(crypto=crypto, apikey=apikey).first()
#             if wallet:
#                 g.user = User.query.get(1)


@bp.route("/login", methods=("GET", "POST"))
@limiter.limit(
    "10 per 15 minutes",
    methods=["POST"],
    key_func=_login_rate_key,
)
@limiter.limit("60 per hour", methods=["POST"])
def login():
    """Log in a registered user by adding the user id to the session."""
    if request.method == "POST":
        username = request.form["name"]
        password = request.form["password"]
        error = None

        user = User.query.filter_by(username=username).first()

        if user is None:
            error = "Incorrect username."
        elif not password:
            error = "No password provided."
        elif not user.verify_password(password):
            error = "Incorrect password."
        elif user.store_id:
            store = Store.query.get(user.store_id)
            if not store or store.status != StoreStatus.ACTIVE:
                error = "Store is not active."

        if error is None:
            # Check if 2FA is enabled
            if user.totp_enabled:
                if _twofa_lock_active(user):
                    session.clear()
                    flash("Too many invalid codes. Please log in again later.")
                    return render_template("auth/login.j2")
                # Store user temporarily for 2FA verification
                session.clear()
                session["pending_user_id"] = user.id
                session["pending_user_time"] = datetime.now().isoformat()
                return redirect(url_for("auth.verify_2fa"))

            # Normal login without 2FA
            session.clear()
            session["user_id"] = user.id
            return redirect(url_for("wallet.show_unlock"))

        flash(error)
    else:
        if not User.query.get(1).passhash:
            return redirect(url_for("auth.set_password"))

    return render_template("auth/login.j2")


@bp.route("/set-password", methods=("GET", "POST"))
def set_password():
    admin = User.query.get(1)
    if admin.passhash:
        return redirect(url_for("auth.login"))
    if request.method == "POST":
        if request.form["pw1"] == request.form["pw2"]:
            admin.passhash = User.get_password_hash(request.form["pw1"])
            db.session.commit()
            return redirect(url_for("auth.login"))
        else:
            flash("Passwords doesn't match")
    return render_template("auth/set-password.j2")


@bp.route("/logout")
def logout():
    """Clear the current session, including the stored user id."""
    session.clear()
    return redirect(url_for("auth.login"))


@bp.route("/2fa/verify", methods=("GET", "POST"))
@limiter.limit("20 per 15 minutes", methods=["POST"])
def verify_2fa():
    """Verify 2FA token after password authentication."""
    pending_user_id = session.get("pending_user_id")
    if not pending_user_id:
        flash("Please log in first.")
        return redirect(url_for("auth.login"))

    user = User.query.get(pending_user_id)
    if not user:
        session.clear()
        return redirect(url_for("auth.login"))

    if _twofa_lock_active(user):
        session.clear()
        flash("Too many invalid codes. Please log in again later.")
        return redirect(url_for("auth.login"))

    # Check session timeout (5 minutes)
    pending_time_str = session.get("pending_user_time")
    if pending_time_str:
        pending_time = datetime.fromisoformat(pending_time_str)
        if datetime.now() - pending_time > timedelta(minutes=5):
            session.pop("pending_user_id", None)
            session.pop("pending_user_time", None)
            flash("Session expired. Please log in again.")
            return redirect(url_for("auth.login"))

    if request.method == "POST":
        token = request.form.get("token", "").replace(" ", "").replace("-", "")
        use_backup = request.form.get("use_backup") == "1"

        verified = False
        if use_backup:
            verified = user.verify_backup_code(token)
            if verified:
                flash(
                    "Backup code used successfully. Please generate new backup codes.",
                    "warning",
                )
        else:
            verified = user.verify_totp(token)

        if verified:
            _clear_twofa_failures(user)
            session.pop("pending_user_id", None)
            session.pop("pending_user_time", None)
            session["user_id"] = user.id
            app.logger.info("2FA success for user id=%s", user.id)
            return redirect(url_for("wallet.show_unlock"))

        if _register_twofa_failure(user):
            session.pop("pending_user_id", None)
            session.pop("pending_user_time", None)
            flash("Too many invalid codes. Please log in again later.")
            return redirect(url_for("auth.login"))
        flash("Invalid authentication code. Please try again.")

    return render_template("auth/verify-2fa.j2")


@bp.route("/2fa/setup", methods=("GET", "POST"))
@login_required
def setup_2fa():
    """Set up 2FA for the current user."""
    user = g.user

    if user.totp_enabled:
        flash("Two-factor authentication is already enabled.")
        return redirect(url_for("wallet.wallets"))  # Redirect to main page

    if request.method == "POST":
        token = request.form.get("token", "").replace(" ", "").replace("-", "")

        # Verify the token with the temporary secret
        temp_secret = session.get("temp_totp_secret")
        if not temp_secret:
            flash("Session expired. Please try again.")
            return redirect(url_for("auth.setup_2fa"))

        totp = pyotp.TOTP(temp_secret)
        if totp.verify(token, valid_window=1):
            # Enable 2FA
            user.totp_secret = temp_secret
            user.totp_enabled = True
            user.totp_enabled_at = datetime.now()

            # Generate backup codes
            backup_codes = user.generate_backup_codes()
            db.session.commit()

            session.pop("temp_totp_secret", None)

            return render_template(
                "auth/2fa-backup-codes.j2", backup_codes=backup_codes
            )
        else:
            flash("Invalid code. Please try again.")
            # Use the existing secret from session instead of generating a new one
            secret = temp_secret
    else:
        # Generate new secret only for initial GET request or when no secret exists
        secret = session.get("temp_totp_secret")
        if not secret:
            secret = pyotp.random_base32()
            session["temp_totp_secret"] = secret

    # Generate provisioning URI
    totp = pyotp.TOTP(secret)
    uri = totp.provisioning_uri(name=user.username, issuer_name="SHKeeper.io")

    # Generate QR code using segno
    qr = segno.make(uri)
    buffer = io.BytesIO()
    qr.save(buffer, kind="png", scale=5)
    qr_base64 = base64.b64encode(buffer.getvalue()).decode()

    return render_template("auth/setup-2fa.j2", secret=secret, qr_code=qr_base64)


@bp.route("/2fa/disable", methods=("GET", "POST"))
@login_required
def disable_2fa():
    """Disable 2FA for the current user."""
    user = g.user

    if not user.totp_enabled:
        flash("Two-factor authentication is not enabled.")
        return redirect(url_for("wallet.settings"))

    if request.method == "POST":
        password = request.form.get("password")
        token = request.form.get("token", "").replace(" ", "").replace("-", "")

        if not user.verify_password(password):
            flash("Incorrect password.")
        elif not user.verify_totp(token):
            flash("Invalid 2FA code.")
        else:
            user.totp_secret = None
            user.totp_enabled = False
            user.backup_codes = None
            user.totp_enabled_at = None
            db.session.commit()
            flash("Two-factor authentication has been disabled.", "success")
            return redirect(url_for("wallet.settings"))

    return render_template("auth/disable-2fa.j2")


@bp.route("/2fa/regenerate-backup", methods=("GET", "POST"))
@login_required
def regenerate_backup_codes():
    """Regenerate backup codes for the current user."""
    user = g.user

    if not user.totp_enabled:
        flash("Two-factor authentication is not enabled.")
        return redirect(url_for("wallet.list_crypto"))

    if request.method == "POST":
        password = request.form.get("password")
        token = request.form.get("token", "").replace(" ", "").replace("-", "")

        if not user.verify_password(password):
            flash("Incorrect password.")
        elif not user.verify_totp(token):
            flash("Invalid 2FA code.")
        else:
            backup_codes = user.generate_backup_codes()
            db.session.commit()
            return render_template(
                "auth/2fa-backup-codes.j2", backup_codes=backup_codes, regenerate=True
            )

    return render_template("auth/regenerate-backup.j2")
