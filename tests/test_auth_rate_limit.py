"""Rate limits on authentication surfaces that do not touch the database."""
from __future__ import annotations

import unittest

from flask import Flask
from flask_limiter.errors import RateLimitExceeded
from flask_limiter.util import get_qualified_name

from shkeeper import limiter, rate_limit_exceeded
from shkeeper.auth import api_key_required, bp


def _probe():
    return {"status": "success"}


probe = api_key_required(_probe)


class TestAuthRateLimit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = Flask("shkeeper_auth_test")
        app.config.update(SECRET_KEY="test-secret", TESTING=True)
        limiter.init_app(app)
        app.register_blueprint(bp)
        app.add_url_rule("/api/v1/probe", view_func=probe)
        app.add_url_rule("/unlimited", view_func=lambda: {"status": "success"})
        app.register_error_handler(RateLimitExceeded, rate_limit_exceeded)
        cls.app = app

    def setUp(self):
        limiter.reset()
        self.client = self.app.test_client()

    def test_api_key_guessing_is_rate_limited(self):
        for _ in range(120):
            response = self.client.get("/api/v1/probe")
            self.assertEqual(response.status_code, 200)
        blocked = self.client.get("/api/v1/probe")
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked.get_json()["status"], "error")

    def test_routes_without_a_limit_stay_open(self):
        for _ in range(130):
            response = self.client.get("/unlimited")
            self.assertEqual(response.status_code, 200)

    def test_unlock_route_carries_a_tight_limit(self):
        from shkeeper.wallet import process_unlock

        limits = limiter.limit_manager.decorated_limits(
            get_qualified_name(process_unlock)
        )
        rendered = " ".join(str(item.limit) for item in limits)
        self.assertIn("10 per 15 minute", rendered)
