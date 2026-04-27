import os
from pathlib import Path


DEFAULT_MODEL = os.getenv("DEFAULT_MODEL_NAME", "gemini-2.5-flash")


def load_env_file(path: str | Path = ".env") -> None:
    """Load simple KEY=VALUE pairs without requiring python-dotenv."""
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def get_model_name() -> str:
    load_env_file()
    return os.getenv("DEFAULT_MODEL_NAME", DEFAULT_MODEL)


def get_client():
    load_env_file()
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("Missing GEMINI_API_KEY. Set it in .env or the environment.")

    try:
        from google import genai
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "google-genai is not installed. Install it with: pip install google-genai"
        ) from exc

    return genai.Client(api_key=api_key)
