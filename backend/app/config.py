import io
import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

# Google's recommended model for new projects (older 2.5 models are now limited
# to accounts that already used them). Override with GEMINI_MODEL in .env.
DEFAULT_MODEL = "gemini-3.8-flash"

ROOT = Path(__file__).resolve().parents[2]  # the folder that contains app.py
ENV_PATH = ROOT / ".env"


def _load_env_file():
    """Read .env from the project folder, whatever editor or shell created it.

    Handles UTF-8 with a BOM and the UTF-16 files PowerShell's ">" writes, and lets
    the file win over an empty variable already set in the environment.
    """
    if not ENV_PATH.exists():
        load_dotenv()  # fall back to python-dotenv's own search
        return
    raw = ENV_PATH.read_bytes()
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        return
    for key, value in dotenv_values(stream=io.StringIO(text)).items():
        if value and value.strip():
            os.environ[key.strip()] = value.strip()


_load_env_file()

GEMINI_API_KEY = (
    os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
).strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "").strip() or DEFAULT_MODEL


def env_problem() -> str:
    """Say why no key was found, so the app can show something actionable."""
    if GEMINI_API_KEY:
        return ""
    if not ENV_PATH.exists():
        if (ROOT / ".env.txt").exists():
            return "Found a file named .env.txt. Rename it to .env (no .txt at the end)."
        return "No .env file was found in the project folder."
    return (
        "A .env file exists, but it has no GEMINI_API_KEY value. "
        "The line must look like: GEMINI_API_KEY=your_key"
    )
