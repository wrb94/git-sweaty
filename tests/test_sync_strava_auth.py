import os
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from unittest import mock
from contextlib import ExitStack


ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS_DIR = os.path.join(ROOT_DIR, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)


requests_stub = types.ModuleType("requests")


class _RequestException(Exception):
    pass


class _HTTPError(_RequestException):
    def __init__(self, message: str, response=None):
        super().__init__(message)
        self.response = response


def _default_request(*_args, **_kwargs):
    raise NotImplementedError("requests.request stub was not patched")


requests_stub.RequestException = _RequestException
requests_stub.HTTPError = _HTTPError
requests_stub.request = _default_request
sys.modules.setdefault("requests", requests_stub)

yaml_stub = types.ModuleType("yaml")
yaml_stub.safe_load = lambda *_args, **_kwargs: {}
sys.modules.setdefault("yaml", yaml_stub)

import sync_strava  # noqa: E402
from strava_gateway import gateway_settings, normalize_gateway_url  # noqa: E402


class _MockResponse:
    def __init__(self, status_code: int, payload: dict | None = None, headers: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise sync_strava.requests.HTTPError(f"{self.status_code} error", response=self)

    def json(self) -> dict:
        return self._payload


class SyncStravaAuthTests(unittest.TestCase):
    def test_direct_strava_still_refreshes_and_retries_after_401(self) -> None:
        config = {"source": "strava"}
        with (
            mock.patch("sync_strava.requests.request", side_effect=[
                _MockResponse(401), _MockResponse(200, {"id": 42}),
            ]) as request,
            mock.patch("sync_strava._get_access_token", return_value="fresh-token") as refresh,
            mock.patch("builtins.print"),
        ):
            athlete, token = sync_strava._run_with_token_refresh(
                config, "expired-token", None, "athlete",
                lambda value: sync_strava._fetch_athlete(value, None, config),
            )
        self.assertEqual(athlete, {"id": 42})
        self.assertEqual(token, "fresh-token")
        refresh.assert_called_once_with(config, None, force_refresh=True)
        self.assertEqual(request.call_args.args, ("GET", "https://www.strava.com/api/v3/athlete"))
        self.assertEqual(request.call_args.kwargs["headers"]["Authorization"], "Bearer fresh-token")

    def test_request_json_with_retry_non_transient_http_fails_fast(self) -> None:
        response = _MockResponse(400, {"message": "Bad Request"})
        with mock.patch("sync_strava.requests.request", return_value=response) as request_mock:
            with self.assertRaises(sync_strava.requests.HTTPError):
                sync_strava._request_json_with_retry(
                    "POST",
                    "https://www.strava.com/oauth/token",
                    limiter=None,
                    request_kind="overall",
                )
        self.assertEqual(request_mock.call_count, 1)

    def test_get_access_token_falls_back_to_configured_refresh_token(self) -> None:
        config = {
            "strava": {
                "client_id": "id",
                "client_secret": "secret",
                "refresh_token": "configured-token",
            }
        }
        cache = {
            "access_token": "expired-token",
            "expires_at": 0,
            "refresh_token": "cached-stale-token",
        }
        calls: list[str] = []

        def _fake_request(_method: str, _url: str, **kwargs):
            candidate = kwargs["data"]["refresh_token"]
            calls.append(candidate)
            if candidate == "cached-stale-token":
                raise sync_strava.requests.HTTPError("400", response=_MockResponse(400))
            return {
                "access_token": "fresh-access",
                "expires_at": 9999999999,
                "refresh_token": "rotated-token",
            }

        saved_payloads: list[dict] = []
        with (
            mock.patch("sync_strava._load_token_cache", return_value=cache),
            mock.patch("sync_strava._request_json_with_retry", side_effect=_fake_request),
            mock.patch("sync_strava._save_token_cache", side_effect=lambda payload: saved_payloads.append(payload)),
            mock.patch("builtins.print"),
            mock.patch(
                "sync_strava.utc_now",
                return_value=datetime(2026, 2, 13, tzinfo=timezone.utc),
            ),
        ):
            token = sync_strava._get_access_token(config, limiter=None)

        self.assertEqual(token, "fresh-access")
        self.assertEqual(calls, ["cached-stale-token", "configured-token"])
        self.assertEqual(saved_payloads[0]["refresh_token"], "rotated-token")

    def test_save_token_cache_persists_refresh_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            token_cache = os.path.join(tmpdir, ".strava_token.json")
            with mock.patch("sync_strava.TOKEN_CACHE", token_cache):
                sync_strava._save_token_cache(
                    {
                        "access_token": "a",
                        "expires_at": 123,
                        "refresh_token": "r",
                    }
                )

                payload = sync_strava._load_token_cache()
                self.assertEqual(payload.get("refresh_token"), "r")


class StravaGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = {
            "source": "strava-gateway",
            "strava_gateway": {"url": "https://gateway.example.com", "client_key": "client-key"},
            "sync": {"recent_days": 0, "per_page": 2, "start_date": "2020-01-01"},
            "rate_limits": {"min_interval_seconds": 0},
            "strava": {"include_activity_urls": True},
        }
        env_patch = mock.patch.dict(os.environ, {"STRAVA_GATEWAY_URL": "", "STRAVA_GATEWAY_CLIENT_KEY": ""})
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def test_gateway_urls_accept_origin_prefix_and_local_development(self) -> None:
        for value, expected in [
            ("https://gateway.example.com/", "https://gateway.example.com"),
            ("https://gateway.example.com/v1/strava/", "https://gateway.example.com"),
            ("http://localhost:8080", "http://localhost:8080"),
            ("http://127.0.0.1:8080", "http://127.0.0.1:8080"),
        ]:
            with self.subTest(value=value):
                self.assertEqual(normalize_gateway_url(value), expected)

    def test_gateway_urls_reject_insecure_or_ambiguous_destinations(self) -> None:
        for value in [
            "", "http://gateway.example.com", "https://key@gateway.example.com",
            "https://gateway.example.com/admin", "https://gateway.example.com?key=secret",
            "https://gateway.example.com#fragment", "https://gateway.example.com?",
            "https://gateway.example.com:invalid", "https://gateway.example.com\\evil",
        ]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_gateway_url(value)

    def test_gateway_credentials_require_url_and_key_and_support_environment(self) -> None:
        with self.assertRaisesRegex(ValueError, "STRAVA_GATEWAY_CLIENT_KEY"):
            gateway_settings({})
        with mock.patch.dict(os.environ, {
            "STRAVA_GATEWAY_URL": "https://env.example.com/v1/strava",
            "STRAVA_GATEWAY_CLIENT_KEY": "env-key",
        }):
            self.assertEqual(gateway_settings(self.config), ("https://env.example.com/v1/strava", "env-key"))

    def test_gateway_auth_errors_never_refresh_oauth(self) -> None:
        for status, message in [(401, "STRAVA_GATEWAY_CLIENT_KEY"), (428, "/admin/strava/authorize")]:
            with (
                self.subTest(status=status),
                mock.patch("sync_strava._get_access_token") as refresh,
                mock.patch("sync_strava.requests.request", return_value=_MockResponse(status)) as request,
                self.assertRaisesRegex(RuntimeError, message),
            ):
                try:
                    sync_strava._run_with_token_refresh(
                        self.config, "client-key", None, "test",
                        lambda token: sync_strava._fetch_page(token, 2, 1, 0, None, None, self.config),
                    )
                finally:
                    refresh.assert_not_called()
                    self.assertEqual(request.call_count, 1)

    def test_gateway_redirects_fail_without_following(self) -> None:
        with (
            mock.patch("sync_strava.requests.request", return_value=_MockResponse(302)) as request,
            self.assertRaisesRegex(RuntimeError, "redirected"),
        ):
            try:
                sync_strava._fetch_athlete("client-key", None, self.config)
            finally:
                self.assertFalse(request.call_args.kwargs["allow_redirects"])

    def test_gateway_pipeline_paginates_generates_strava_links_and_resumes(self) -> None:
        import run_pipeline

        activities = [
            {"id": 123, "start_date_local": "2026-10-01T10:00:00Z", "type": "Run", "distance": 5000},
            {"id": 124, "start_date_local": "2026-10-02T10:00:00Z", "type": "Ride", "distance": 10000},
        ]

        def respond(method, url, **kwargs):
            self.assertEqual(method, "GET")
            self.assertTrue(url.startswith("https://gateway.example.com/v1/strava/"))
            self.assertEqual(kwargs["headers"], {"Authorization": "Bearer client-key"})
            self.assertFalse(kwargs["allow_redirects"])
            if url.endswith("/athlete"):
                return _MockResponse(200, {"id": 42})
            params = kwargs["params"]
            self.assertEqual(params["per_page"], 2)
            self.assertGreater(params["after"], 0)
            self.assertIn("before", params)
            return _MockResponse(200, activities if params["page"] == 1 else [])

        previous_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmpdir, ExitStack() as stack:
            os.chdir(tmpdir)
            stack.callback(os.chdir, previous_cwd)
            for module in ["run_pipeline", "sync_strava", "normalize", "aggregate", "generate_heatmaps"]:
                stack.enter_context(mock.patch(f"{module}.load_config", return_value=self.config))
            request = stack.enter_context(mock.patch("sync_strava.requests.request", side_effect=respond))
            cache_read = stack.enter_context(mock.patch("sync_strava._load_token_cache"))
            cache_write = stack.enter_context(mock.patch("sync_strava._save_token_cache"))
            run_pipeline.run_pipeline(False, False, False, False)
            with open("site/data.json") as handle:
                dashboard = json.load(handle)
            self.assertEqual(dashboard["source"], "strava-gateway")
            self.assertEqual({item["url"] for item in dashboard["activities"]}, {
                "https://www.strava.com/activities/123", "https://www.strava.com/activities/124",
            })
            self.assertTrue(os.path.exists("activities/raw/strava/123.json"))
            with open("data/backfill_state_strava.json") as handle:
                self.assertTrue(json.load(handle)["completed"])
            self.assertEqual([call.kwargs["params"]["page"] for call in request.call_args_list if "params" in call.kwargs], [1, 2])
            request.reset_mock()
            run_pipeline.run_pipeline(False, False, False, False)
            # A completed backfill only needs the athlete lookup when recent sync is disabled.
            self.assertEqual(request.call_count, 1)
            cache_read.assert_not_called()
            cache_write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
