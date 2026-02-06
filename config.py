import importlib
import os
from pathlib import Path

_ENV_LOADED = False


def _load_env() -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    try:
        dotenv = importlib.import_module("dotenv")
    except Exception as exc:  # pragma: no cover - defensive
        raise SystemExit(
            "python-dotenv is required (pip install -r requirements.txt)"
        ) from exc
    env_path = Path(__file__).resolve().parent / ".env"
    dotenv.load_dotenv(dotenv_path=env_path)
    _ENV_LOADED = True


def get_env(
    name: str, default: str | None = None, required: bool = False
) -> str | None:
    _load_env()
    value = os.environ.get(name, default)
    if required and (value is None or value == ""):
        raise SystemExit(f"{name} env var is required")
    return value


BINANCE_CONFIG = {
    "apiKey": get_env("BINANCE_TESTNET_API_KEY"),
    "secret": get_env("BINANCE_TESTNET_SECRET"),
    "sandbox": True,
    "options": {"defaultType": "spot"},
    "enableRateLimit": True,
}
