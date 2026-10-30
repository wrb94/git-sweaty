import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/sync.yml"


def step_script(name):
    section = WORKFLOW.read_text().split(f"      - name: {name}\n", 1)[1]
    section = section.split("\n      - name:", 1)[0]
    return textwrap.dedent(section.split("        run: |\n", 1)[1])


class GatewayWorkflowTests(unittest.TestCase):
    def test_gateway_source_resolves_and_generates_config_without_oauth_credentials(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {**os.environ, "WORKFLOW_SOURCE": "strava-gateway", "DASHBOARD_SOURCE": "",
                   "GITHUB_ENV": f"{tmpdir}/env", "GITHUB_OUTPUT": f"{tmpdir}/output",
                   "SYNC_SOURCE": "strava-gateway", "STRAVA_GATEWAY_URL": "https://gateway.example.com",
                   "STRAVA_GATEWAY_CLIENT_KEY": "client-key", "DASHBOARD_STRAVA_ACTIVITY_LINKS": "true",
                   "DASHBOARD_STRAVA_PROFILE_URL": "https://www.strava.com/athletes/42"}
            for name in ["Resolve source", "Validate required source secrets", "Write config.local.yaml"]:
                result = subprocess.run(["bash", "-e", "-c", step_script(name)], cwd=tmpdir,
                                        env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("source=strava-gateway", Path(tmpdir, "output").read_text())
            config = Path(tmpdir, "config.local.yaml").read_text()
            self.assertIn('source: "strava-gateway"', config)
            self.assertIn("include_activity_urls: true", config)
            self.assertIn('profile_url: "https://www.strava.com/athletes/42"', config)
            self.assertNotIn("client_secret:", config)
            self.assertNotIn("client-key", config)

    def test_gateway_validation_requires_its_client_key(self):
        env = {**os.environ, "SYNC_SOURCE": "strava-gateway",
               "STRAVA_GATEWAY_URL": "https://gateway.example.com", "STRAVA_GATEWAY_CLIENT_KEY": ""}
        result = subprocess.run(["bash", "-c", step_script("Validate required source secrets")],
                                env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("STRAVA_GATEWAY_CLIENT_KEY", result.stdout)


if __name__ == "__main__":
    unittest.main()
