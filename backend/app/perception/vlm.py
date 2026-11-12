import logging

from google import genai
from google.genai import types

from ..config import GEMINI_API_KEY, GEMINI_MODEL
from ..nutrition.foods import load_foods
from .models import VisionResult

logger = logging.getLogger(__name__)

_client = None

PROMPT = """You identify foods in a photo for a Bangladeshi nutrition app. A nutrient table (the FOOD LIST below) is the ONLY source of numbers, so you must describe what you see in terms of that list.
Rules:
1. List every food clearly visible. Never invent items you cannot see.
2. A single ingredient (rice, an egg, a fish): set dish to an empty string and choose the best matching code.
3. A prepared dish (fried rice, chicken curry, soup): break it into its main ingredients. Create one item per ingredient, set dish to the dish name, choose the closest code for each ingredient, and give each ingredient its own estimated_grams so the parts add up to the visible portion.
4. If a dish looks oily or fried, add the cooking fat (for example an oil or ghee, if it is in the list) as a separate item with a modest estimate (5 to 15 g per portion).
5. Prefer a cooked or prepared list entry when one exists; otherwise use the raw entry and set state to the real condition shown.
6. Use an empty fct_code only when nothing in the list is a reasonable stand-in (for example a packaged sausage); then describe it in name_en.
7. Ignore sauces in small cups, garnish, plates, cutlery and drinks unless they are a meaningful portion of food.
8. confidence is your honest certainty from 0 to 1. Use lower values for mixed dishes and for estimated weights.
9. If the image is a printed menu, set scene_type to "menu" and return no items.
10. If the image is too dark, blurry, or shows no food, set scene_type to "unusable" or "other", explain in quality_issue, and return no items.
11. Never omit a main component of the meal (noodles, rice, bread, meat, fish). If no list entry is a reasonable stand-in, still return the item with an empty fct_code, its real name in name_en, and an estimated_grams, so the user can confirm it. If the closest list entry is a clearly different food, prefer the empty code to a wrong match.

FOOD LIST (code | name):
{vocab}
"""


def _http_options() -> types.HttpOptions:
    """Time out instead of hanging, and retry Google's temporary 5xx errors."""
    options = {"timeout": 90_000}  # milliseconds
    retry = getattr(types, "HttpRetryOptions", None)
    if retry is not None:
        options["retry_options"] = retry(
            attempts=3, initial_delay=1.0, http_status_codes=[500, 502, 503, 504]
        )
    try:
        return types.HttpOptions(**options)
    except Exception:  # older SDKs without retry support
        return types.HttpOptions(timeout=options["timeout"])


def _get_client():
    global _client
    if _client is None:
        if not GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY is not set in .env")
        _client = genai.Client(api_key=GEMINI_API_KEY, http_options=_http_options())
    return _client


def explain_failure(exc: Exception) -> str:
    """Turn an API or network error into a short message the user can act on."""
    if isinstance(exc, RuntimeError) and "GEMINI_API_KEY" in str(exc):
        return (
            "No Gemini API key was found. Add GEMINI_API_KEY to the .env file "
            "in your project folder, then restart the app."
        )

    code = getattr(exc, "code", None)
    detail = str(getattr(exc, "message", "") or exc)
    if GEMINI_API_KEY:
        detail = detail.replace(GEMINI_API_KEY, "***")  # never echo the key
    detail = " ".join(detail.split())[:200]
    lowered = detail.lower()

    if code == 401 or (code in (400, 403) and "api key" in lowered):
        return (
            "Google rejected the API key. Check GEMINI_API_KEY in .env: it should "
            "be the full key, with no spaces or extra characters."
        )
    if code == 403:
        return f"Google denied access for this key or model. Details: {detail}"
    if code == 404:
        other = "gemini-3.5-flash-lite" if GEMINI_MODEL == "gemini-3.8-flash" else "gemini-3.8-flash"
        return (
            f"The model '{GEMINI_MODEL}' is not available to this key. Set "
            f"GEMINI_MODEL in .env to another current model, for example {other}."
        )
    if code == 429:
        return (
            "The Gemini quota or rate limit was reached. Wait a minute and try "
            "again, or check your quota in Google AI Studio."
        )
    if isinstance(code, int) and code >= 500:
        return "Google's Gemini service is busy or temporarily down. Please try again in a moment."
    if code == 400:
        return f"Gemini could not process this request. Details: {detail}"
    if isinstance(exc, ValueError):  # pydantic: the model's reply didn't match the schema
        return "Gemini returned an answer in an unexpected format. Please try again."
    if isinstance(exc, TimeoutError) or "timed out" in lowered or "timeout" in lowered:
        return "The request to Gemini timed out. Check your internet connection and try again."
    if any(w in lowered for w in ("connect", "resolution", "network", "unreachable")):
        return "Could not reach Gemini. Check your internet connection."
    return f"Unexpected error ({type(exc).__name__})."


def check_connection() -> tuple[bool, str]:
    """Send a tiny text request so users can verify their key and model."""
    try:
        _get_client().models.generate_content(
            model=GEMINI_MODEL, contents="Reply with the single word: ok"
        )
    except Exception as exc:
        logger.warning("Gemini connection test failed: %r", exc)
        return False, explain_failure(exc)
    return True, f"Connected. Photo analysis will use {GEMINI_MODEL}."


def _vocabulary() -> str:
    df = load_foods()
    return "\n".join(f"{c} | {n}" for c, n in zip(df["code"], df["name_en"]))


def detect_foods(image_bytes: bytes, mime_type: str = "image/jpeg") -> VisionResult:
    resp = _get_client().models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            PROMPT.format(vocab=_vocabulary()),
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=VisionResult,
            temperature=0.1,
        ),
    )
    if getattr(resp, "parsed", None) is not None:
        return resp.parsed
    return VisionResult.model_validate_json(resp.text)