from __future__ import annotations

import json
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from backend.config.settings import Settings

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[2]
SECRET_WORDS = ("SECRET", "KEY", "PASSWORD", "TOKEN", "DSN")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in Settings.model_fields:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="module")
def blueprint() -> dict:
    return yaml.safe_load((ROOT / "render.yaml").read_text())


def service(blueprint: dict, name: str) -> dict:
    return next(s for s in blueprint["services"] if s["name"] == name)


def env_of(svc: dict) -> dict[str, dict]:
    return {e["key"]: e for e in svc.get("envVars", [])}


# ------------------------------------------------------------------------------ render.yaml
def test_the_blueprint_creates_exactly_the_four_expected_resources(blueprint):
    names = {d["name"] for d in blueprint["databases"]} | {s["name"] for s in blueprint["services"]}
    assert names == {"slothpay-db", "slothpay-redis", "slothpay-api", "slothpay-web"}


def test_every_resource_is_on_the_free_plan(blueprint):
    for resource in [*blueprint["databases"], *blueprint["services"]]:
        assert resource["plan"] == "free", resource["name"]


def test_no_paid_worker_is_active_by_default(blueprint):
    assert all(s["type"] != "worker" for s in blueprint["services"])


def test_the_redis_store_never_evicts_the_duplicate_locks(blueprint):
    redis = service(blueprint, "slothpay-redis")
    assert redis["type"] == "keyvalue"
    assert redis["maxmemoryPolicy"] == "noeviction"
    assert redis["ipAllowList"] == []


def test_the_api_runs_in_demo_mode_with_chaos_off_and_workers_on(blueprint):
    env = env_of(service(blueprint, "slothpay-api"))
    assert env["ENV"]["value"] == "demo"
    assert env["CHAOS_ENABLED"]["value"] == "false"
    assert env["RUN_BACKGROUND_WORKERS"]["value"] == "true"


def test_the_api_only_talks_to_the_paypal_sandbox(blueprint):
    env = env_of(service(blueprint, "slothpay-api"))
    assert env["PAYPAL_BASE_URL"]["value"] == "https://api-m.sandbox.paypal.com"


def test_no_secret_value_is_written_in_the_blueprint(blueprint):
    for svc in blueprint["services"]:
        for key, entry in env_of(svc).items():
            if any(word in key for word in SECRET_WORDS):
                literal = entry.get("value")
                assert literal in (None, ""), f"{svc['name']}: {key} has a literal value"
                assert entry.get("sync") is False or entry.get("generateValue") is True, key


def test_render_generates_the_two_signing_secrets(blueprint):
    env = env_of(service(blueprint, "slothpay-api"))
    assert env["PASSPORT_SIGNING_SECRET"]["generateValue"] is True
    assert env["ADMIN_API_KEY"]["generateValue"] is True


def test_the_paypal_credentials_are_asked_for_not_stored(blueprint):
    env = env_of(service(blueprint, "slothpay-api"))
    for key in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "PAYPAL_WEBHOOK_ID"):
        assert env[key] == {"key": key, "sync": False}


def test_every_api_setting_in_the_blueprint_is_a_real_setting(blueprint):
    known = set(Settings.model_fields)
    for key in env_of(service(blueprint, "slothpay-api")):
        assert key in known, f"{key} is in render.yaml but not in Settings"


def test_the_database_and_redis_come_from_the_created_resources(blueprint):
    env = env_of(service(blueprint, "slothpay-api"))
    assert env["DATABASE_URL"]["fromDatabase"] == {
        "name": "slothpay-db",
        "property": "connectionString",
    }
    redis_ref = env["REDIS_URL"]["fromService"]
    assert redis_ref["name"] == "slothpay-redis" and redis_ref["type"] == "keyvalue"
    assert redis_ref["property"] == "connectionString"


def test_the_api_start_command_migrates_first_then_serves_on_render_port(blueprint):
    api = service(blueprint, "slothpay-api")
    command = " ".join(api["dockerCommand"].split())
    assert command.index("alembic upgrade head") < command.index("uvicorn backend.main:app")
    assert "&&" in command and "exec uvicorn" in command
    assert "--port $PORT" in command
    assert "--proxy-headers" in command
    assert api["healthCheckPath"] == "/health"


def test_the_two_public_addresses_match_the_service_names(blueprint):
    api_env = env_of(service(blueprint, "slothpay-api"))
    web_env = env_of(service(blueprint, "slothpay-web"))
    assert api_env["FRONTEND_ORIGIN"]["value"] == "https://slothpay-web.onrender.com"
    assert web_env["BACKEND_URL"]["value"] == "https://slothpay-api.onrender.com"


def test_the_dashboard_calls_its_own_site_in_the_hosted_demo(blueprint):
    env = env_of(service(blueprint, "slothpay-web"))
    assert env["NEXT_PUBLIC_API_BASE_URL"]["value"] == ""
    assert env["NEXT_PUBLIC_USE_MOCKS"]["value"] == "false"
    assert "NEXT_PUBLIC_ADMIN_API_KEY" not in env, "an admin key must never reach the browser"


def test_the_dockerfile_paths_in_the_blueprint_exist(blueprint):
    for name in ("slothpay-api", "slothpay-web"):
        svc = service(blueprint, name)
        context = ROOT / svc["dockerContext"]
        assert context.is_dir(), name
        assert (context / Path(svc["dockerfilePath"]).relative_to(svc["dockerContext"])).is_file()


def test_the_api_settings_in_the_blueprint_pass_the_demo_startup_guards(blueprint):
    """Build the real Settings from the blueprint's values, plus the secrets Render supplies."""
    values = {
        key: entry["value"]
        for key, entry in env_of(service(blueprint, "slothpay-api")).items()
        if "value" in entry
    }
    values["DATABASE_URL"] = "postgres://u:p@dpg-host/slothpay"  # what Render hands over
    values["PASSPORT_SIGNING_SECRET"] = "Aa1" * 20
    values["ADMIN_API_KEY"] = "Bb2" * 20
    values["PAYPAL_CLIENT_ID"] = "sandbox-id"
    values["PAYPAL_CLIENT_SECRET"] = "sandbox-secret"
    settings = Settings(_env_file=None, **values)
    assert settings.ENV == "demo" and settings.secure_cookies is True
    assert settings.DATABASE_URL.startswith("postgresql+asyncpg://")
    assert settings.RUN_BACKGROUND_WORKERS is True


# ------------------------------------------------------------------------ frontend Dockerfile
def test_the_frontend_image_takes_every_build_value_the_blueprint_sets(blueprint):
    dockerfile = (ROOT / "frontend" / "Dockerfile").read_text()
    args = set(re.findall(r"^ARG (\w+)", dockerfile, flags=re.M))
    for key in env_of(service(blueprint, "slothpay-web")):
        assert key in args, f"render.yaml sets {key} but the Dockerfile has no ARG for it"


def test_the_frontend_image_is_small_and_not_run_as_root():
    dockerfile = (ROOT / "frontend" / "Dockerfile").read_text()
    assert dockerfile.count("FROM node:") == 3, "deps, build and run stages"
    assert "USER node" in dockerfile
    assert 'CMD ["node", "server.js"]' in dockerfile
    assert ".next/standalone" in dockerfile and ".next/static" in dockerfile
    assert "COPY --from=build" in dockerfile and "npm ci" in dockerfile


def test_the_image_never_copies_local_secret_files():
    ignore = (ROOT / "frontend" / ".dockerignore").read_text().split()
    for name in ("node_modules", ".next", ".env", ".env.*", ".git"):
        assert name in ignore
    assert "!.env.example" in ignore


def test_the_standalone_build_the_image_relies_on_is_switched_on():
    config = (ROOT / "frontend" / "next.config.ts").read_text()
    assert 'output: "standalone"' in config
    assert "compress: false" in config, "compression would delay the live status stream"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
class TestNextConfigProxy:
    """Load the real next.config.ts with BACKEND_URL set and unset (Node 22 strips the types)."""

    SCRIPT = (
        "import('file://" + "@@PATH@@" + "').then(async m => {"
        "const r = m.default.rewrites ? await m.default.rewrites() : [];"
        "console.log(JSON.stringify(r));})"
        ".catch(e => {console.error(e.message); process.exit(3);})"
    )

    def run(self, backend_url: str | None):
        node = shutil.which("node")
        env = {"PATH": str(Path(node).parent), "HOME": "/tmp"}
        if backend_url is not None:
            env["BACKEND_URL"] = backend_url
        path = ROOT / "frontend" / "next.config.ts"
        return subprocess.run(
            [
                "node",
                "--experimental-strip-types",
                "-e",
                self.SCRIPT.replace("@@PATH@@", str(path)),
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

    def test_unset_means_no_proxy(self):
        done = self.run(None)
        assert done.returncode == 0, done.stderr
        assert json.loads(done.stdout.strip().splitlines()[-1]) == []

    def test_set_forwards_api_calls_and_ignores_a_trailing_slash(self):
        done = self.run("https://slothpay-api.onrender.com/")
        assert done.returncode == 0, done.stderr
        assert json.loads(done.stdout.strip().splitlines()[-1]) == [
            {
                "source": "/api/:path*",
                "destination": "https://slothpay-api.onrender.com/api/:path*",
            }
        ]

    @pytest.mark.parametrize(
        "bad", ["slothpay-api.onrender.com", "https://host/some/path", "ftp://x"]
    )
    def test_a_malformed_address_stops_the_build(self, bad):
        done = self.run(bad)
        assert done.returncode == 3
        assert "BACKEND_URL must look like" in done.stderr


# --------------------------------------------------------------------------- .env.example
def env_example_keys() -> dict[str, str]:
    out = {}
    for line in (ROOT / ".env.example").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


def test_every_key_in_env_example_is_a_real_setting():
    for key in env_example_keys():
        assert key in Settings.model_fields, f"{key} in .env.example is not a setting"


def test_env_example_lists_the_new_worker_settings_with_the_real_defaults():
    keys = env_example_keys()
    defaults = Settings(_env_file=None)
    assert keys["RUN_BACKGROUND_WORKERS"] == "false"
    assert (
        float(keys["BACKGROUND_WORKER_INTERVAL_SECONDS"])
        == defaults.BACKGROUND_WORKER_INTERVAL_SECONDS
    )


def test_env_example_holds_only_placeholders_for_secrets():
    for key, value in env_example_keys().items():
        if any(word in key for word in SECRET_WORDS):
            assert value == "" or value.startswith(("your-", "change-me")), (
                f"{key} must be empty or an obvious placeholder"
            )


def test_env_example_works_as_a_local_settings_file(tmp_path):
    """Copying it to .env must give a valid local configuration."""
    target = tmp_path / ".env"
    target.write_text((ROOT / ".env.example").read_text())
    settings = Settings(_env_file=target)
    assert settings.ENV == "local" and settings.RUN_BACKGROUND_WORKERS is False


# ---------------------------------------------------------------------------- README and docs
def test_every_backend_variable_in_the_readme_table_exists_with_the_stated_default():
    readme = (ROOT / "README.md").read_text()
    section = readme.split("Backend settings", 1)[1].split("Frontend settings", 1)[0]
    rows = re.findall(r"^\| `([A-Z_]+)`(?:, `([A-Z_]+)`)* \| (.*?) \|", section, flags=re.M)
    assert len(rows) >= 15
    defaults = Settings(_env_file=None)
    for name in re.findall(r"^\| `([A-Z_]+)`", section, flags=re.M):
        assert name in Settings.model_fields, f"README documents {name}, which is not a setting"
    for name, shown in {
        "SESSION_TTL_HOURS": "12",
        "MAX_REQUEST_BYTES": "65536",
        "CONDITION_MAX_HOURS": "48",
        "RATE_LIMIT_PAYMENTS_PER_MINUTE": "30",
        "RATE_LIMIT_WEBHOOK_PER_MINUTE": "300",
        "RATE_LIMIT_LOGIN_PER_MINUTE": "20",
        "BACKGROUND_WORKER_INTERVAL_SECONDS": "5",
    }.items():
        value = getattr(defaults, name)
        assert (f"{value:g}" if isinstance(value, float) else str(value)) == shown, name
        assert re.search(rf"`{name}` \| `{shown}`", section), f"README default for {name}"


def test_every_setting_a_user_must_know_is_in_the_readme_table():
    readme = (ROOT / "README.md").read_text()
    for name in (
        "ENV",
        "DATABASE_URL",
        "REDIS_URL",
        "PAYPAL_CLIENT_ID",
        "PAYPAL_CLIENT_SECRET",
        "PAYPAL_BASE_URL",
        "PAYPAL_WEBHOOK_ID",
        "FRONTEND_ORIGIN",
        "PASSPORT_SIGNING_SECRET",
        "ADMIN_API_KEY",
        "CHAOS_ENABLED",
        "RUN_BACKGROUND_WORKERS",
        "BACKGROUND_WORKER_INTERVAL_SECONDS",
        "NEXT_PUBLIC_API_BASE_URL",
        "NEXT_PUBLIC_USE_MOCKS",
        "NEXT_PUBLIC_ADMIN_API_KEY",
        "BACKEND_URL",
    ):
        assert f"`{name}`" in readme, name


MARKDOWN_FILES = [
    "README.md",
    "docs/deploy.md",
    "docs/architecture.md",
    "docs/demo-script.md",
    "docs/submission.md",
]


@pytest.mark.parametrize("name", MARKDOWN_FILES)
def test_relative_links_and_images_in_the_docs_point_at_real_files(name):
    path = ROOT / name
    assert path.is_file(), name
    for target in re.findall(r"!?\[[^\]]*\]\(([^)\s]+)\)", path.read_text()):
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        assert (path.parent / target.split("#")[0]).exists(), f"{name}: dead link {target}"


@pytest.mark.parametrize("name", MARKDOWN_FILES)
def test_docs_name_only_files_that_exist(name):
    text = (ROOT / name).read_text()
    local_only = {"frontend/.env.local"}  # created by the user, never committed
    for ref in set(re.findall(r"`((?:docs|scripts|tests|backend|frontend)/[\w./\-]+\.\w+)`", text)):
        assert ref in local_only or (ROOT / ref).exists(), (
            f"{name} mentions {ref}, which does not exist"
        )


def test_no_doc_contains_a_secret_or_a_live_paypal_address():
    for name in MARKDOWN_FILES + ["render.yaml", ".env.example"]:
        text = (ROOT / name).read_text()
        assert "api-m.paypal.com" not in text.replace("api-m.sandbox.paypal.com", "")
        assert not re.search(r"A[A-Za-z0-9_-]{60,}", text), f"{name} has a PayPal-like secret"
        assert "BEGIN PRIVATE KEY" not in text


def test_the_architecture_picture_is_valid_svg_with_a_title():
    tree = ET.parse(ROOT / "docs" / "images" / "architecture.svg")
    assert tree.getroot().tag.endswith("svg")
    assert any(el.tag.endswith("title") for el in tree.iter())


def test_the_docs_use_the_real_button_names():
    condition = (ROOT / "frontend/src/components/ConditionPanel.tsx").read_text()
    chaos = (ROOT / "frontend/src/components/ChaosPanel.tsx").read_text()
    text = "\n".join((ROOT / name).read_text() for name in MARKDOWN_FILES)
    for label in ("Verify: condition met", "Fail: condition not met"):
        assert label in condition, f"the button {label!r} is not in the UI any more"
        assert label in text
    for label in ("Kill network", "Double-click test (5 clicks)", "Turn off"):
        assert label in chaos, label
    assert "Kill network" in (ROOT / "docs/demo-script.md").read_text()


def test_the_demo_script_does_not_stage_the_known_declined_capture_gap():
    script = (ROOT / "docs/demo-script.md").read_text()
    assert (
        'Do not demo "Decline"' in script
        or 'Do **not** demo "Decline"' in script
        or ('**Do not demo "Decline"**' in script)
    )
    steps = script.split("| Time |", 1)[1].split("## Before recording", 1)[0]
    assert "**Decline**" not in steps


def test_the_demo_script_fits_in_three_minutes():
    script = (ROOT / "docs/demo-script.md").read_text()
    times = re.findall(r"^\| (\d):(\d\d) \|", script, flags=re.M)
    assert times, "no timed steps"
    seconds = [int(m) * 60 + int(s) for m, s in times]
    assert seconds == sorted(seconds)
    assert seconds[-1] < 180


def test_the_docs_state_the_limits_honestly():
    readme = (ROOT / "README.md").read_text()
    deploy = (ROOT / "docs/deploy.md").read_text()
    assert "sandbox" in readme.lower() and "simulat" in readme.lower()
    assert "declined capture" in readme.lower()
    assert "does not run Temporal" in deploy
    assert "30 days" in deploy and "750" in deploy and "pinger" in deploy
    assert "Never use a real PayPal account" in readme


def test_the_submission_pack_has_the_deadline_and_the_checklist():
    text = (ROOT / "docs/submission.md").read_text()
    for needle in ("12 November 2026", "11 November", "15 December", "AI tools", "sandbox"):
        assert needle in text, needle