"""Nutrition Intelligence: estimate the nutrition of a meal from a photo."""

import base64
import html
import io
import json
import sys
from functools import lru_cache
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from backend.app.perception.pipeline import analyze_image
from backend.app.nutrition.foods import load_foods
from backend.app.nutrition.engine import compute_meal
from backend.app.schemas import MealItem

try:
    from backend.app.config import GEMINI_API_KEY
except Exception:  # if config can't load, let the analyzer report the problem
    GEMINI_API_KEY = "unknown"

API_KEY_MISSING = not GEMINI_API_KEY

PROFILE_FILE = ROOT / "user_profile.json"

EQUATIONS = ["Female equation", "Male equation"]
ACTIVITY_LEVELS = [
    "Sedentary",
    "Lightly active",
    "Moderately active",
    "Very active",
    "Extra active",
]
GOALS = ["Maintain weight", "Gradual weight loss", "Gradual muscle gain"]

DEFAULT_PROFILE = {
    "name": "there",
    "age": 25,
    "height_cm": 165.0,
    "weight_kg": 65.0,
    "equation": EQUATIONS[0],
    "activity": ACTIVITY_LEVELS[0],
    "goal": GOALS[0],
    "dark_mode": False,
}

EQUATION_HELP = (
    "Choose the equation parameter that best fits you. "
    "This is a calculation setting, not a gender identity label."
)
ONBOARDING_STEPS = 8

# Streamlit 1.50+ replaced use_container_width with width="stretch".
_VERSION = tuple(int(p) for p in st.__version__.split(".")[:2] if p.isdigit())
STRETCH = (
    {"width": "stretch"} if _VERSION >= (1, 50) else {"use_container_width": True}
)

esc = html.escape


# --------------------------------------------------
# 1. Profile storage and daily target estimation
# --------------------------------------------------

def load_user_profile():
    """Load the saved user profile, if one exists."""
    if not PROFILE_FILE.exists():
        return None

    try:
        with PROFILE_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (json.JSONDecodeError, OSError):
        return None

    return data if isinstance(data, dict) else None


def save_user_profile(profile):
    """Save the user profile locally as JSON."""
    with PROFILE_FILE.open("w", encoding="utf-8") as file:
        json.dump(profile, file, indent=4)


def _number(value, low, high, default):
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return default


def normalize_profile(raw):
    """Fill gaps and clamp values so a hand-edited file can't break the UI."""
    profile = {**DEFAULT_PROFILE, **(raw or {})}
    profile["name"] = str(profile["name"]).strip() or DEFAULT_PROFILE["name"]
    profile["age"] = int(_number(profile["age"], 18, 100, 25))
    profile["height_cm"] = _number(profile["height_cm"], 91, 244, 165.0)
    profile["weight_kg"] = _number(profile["weight_kg"], 30, 300, 65.0)
    profile["dark_mode"] = bool(profile.get("dark_mode"))

    for key, options in (
        ("equation", EQUATIONS),
        ("activity", ACTIVITY_LEVELS),
        ("goal", GOALS),
    ):
        if profile[key] not in options:
            profile[key] = DEFAULT_PROFILE[key]

    return profile


def estimate_daily_targets(age, height_cm, weight_kg, equation, activity, goal):
    """Approximate adult energy needs using Mifflin-St Jeor."""

    if equation == "Male equation":
        bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age + 5
    else:
        bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age - 161

    activity_factors = {
        "Sedentary": 1.2,
        "Lightly active": 1.375,
        "Moderately active": 1.55,
        "Very active": 1.725,
        "Extra active": 1.9,
    }

    maintenance = bmr * activity_factors[activity]

    goal_adjustments = {
        "Maintain weight": 0,
        "Gradual weight loss": -300,
        "Gradual muscle gain": 200,
    }

    estimated_calories = round(maintenance + goal_adjustments[goal])

    # Conservative demo safeguard; this is not a universal
    # minimum calorie requirement for every adult.
    calories = max(1200, estimated_calories)

    protein = round(1.2 * weight_kg)

    return {
        "calories_kcal": calories,
        "protein_g": protein,
        "maintenance_kcal": round(maintenance),
        "used_calorie_floor": estimated_calories < 1200,
    }


def targets_for(profile):
    return estimate_daily_targets(
        profile["age"],
        profile["height_cm"],
        profile["weight_kg"],
        profile["equation"],
        profile["activity"],
        profile["goal"],
    )


def ft_in_from_cm(height_cm):
    total_inches = round(height_cm / 2.54)
    return total_inches // 12, total_inches % 12


REVIEW_KEY_PREFIXES = ("food_", "grams_", "include_", "mgrams_", "minc_", "mrm_")


def clear_review_state():
    """Forget the current meal: totals, manual foods and old widget values."""
    for key in list(st.session_state.keys()):
        if key.startswith(REVIEW_KEY_PREFIXES):
            del st.session_state[key]
    for key in ("meal_totals", "manual_items", "show_manual"):
        st.session_state.pop(key, None)


def reset_profile():
    """Delete the saved profile and send the user back to first-time setup."""
    try:
        PROFILE_FILE.unlink(missing_ok=True)
    except OSError as exc:
        st.error(f"Could not delete the profile: {exc}")
        return

    clear_review_state()
    for key in ("onboarding_step", "new_profile", "analysis_result", "analysis_filename"):
        st.session_state.pop(key, None)

    st.rerun()


def save_theme_choice():
    """Persist the dark-mode toggle in the saved profile."""
    current = load_user_profile()
    if current is None:
        return
    current["dark_mode"] = bool(st.session_state.get("dark_mode_toggle"))
    try:
        save_user_profile(current)
    except OSError:
        pass  # the choice still applies for this session


# --------------------------------------------------
# 2. Page setup and styling
# --------------------------------------------------

st.set_page_config(
    page_title="Nutrition Intelligence",
    page_icon="🥗",
    layout="wide",
)

CSS_BASE = """
@import url('https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,700&family=Public+Sans:wght@400;500;600&display=swap');
:root {
--ink: #12352A; --muted: #4E6B5F; --line: #CFE0D6; --tint: #F1F7F3; --surface: #FFFFFF; --bg: #FFFFFF; --sidebar: #EAF3EE; --field: #FFFFFF;
--accent: #1E5B45; --accent-text: #1E5B45; --heading: #123D2E; --soft: #8DBBA5; --fat: #8DBBA5;
--marigold: #F5A623; --marigold-tint: #FFF3D6; --over-text: #8A5200;
--track: #DCE8E0; --dot-bg: #E3ECE6; --dot-done: #D3EADF;
--pill-bg: #E4EEE8; --pill-text: #1B4A39; --pill-hi-bg: #D3EADF; --pill-hi-text: #0F4A35; --pill-mid-text: #6B4300;
--hero-a: #1E5B45; --hero-b: #123D2E; --hero-rgb: 18, 61, 46; --hero-end: .08;
}
.stApp, .stApp button, .stApp input, .stApp textarea { font-family: 'Public Sans', system-ui, sans-serif; }
.stApp { background: var(--bg); }
.stApp h1, .stApp h2, .stApp h3, .stApp h4 { font-family: 'Bricolage Grotesque', system-ui, sans-serif; letter-spacing: -0.015em; color: var(--heading); }
.block-container { max-width: 1120px; padding-top: 2rem; padding-bottom: 4rem; }
#MainMenu, footer { visibility: hidden; }
.stApp button:focus-visible, .stApp input:focus-visible { outline: 3px solid var(--marigold); outline-offset: 2px; }
.stButton > button, [data-testid="stFormSubmitButton"] > button { border-radius: 10px; font-weight: 600; padding: 0.55rem 1.3rem; }
[data-testid="stForm"] { background: var(--surface); border: 1px solid var(--line); border-top: 5px solid var(--marigold); border-radius: 14px; padding: 1.4rem; }
[data-testid="stFileUploaderDropzone"] { background: var(--tint); border: 2px dashed var(--accent); border-radius: 14px; }
[data-testid="stCheckbox"] { margin-top: 1.9rem; }
[data-testid="stSidebar"] { border-right: 1px solid var(--line); }
.hero { position: relative; overflow: hidden; display: flex; align-items: center; min-height: 210px; background: url("HERO_PHOTO") center/cover no-repeat, rgb(var(--hero-rgb)); border-radius: 20px; padding: 40px 40px 58px 40px; margin-bottom: 18px; }
.hero::before { content: ""; position: absolute; inset: 0; background: linear-gradient(100deg, rgba(var(--hero-rgb), .96) 0%, rgba(var(--hero-rgb), .86) 40%, rgba(var(--hero-rgb), .45) 72%, rgba(var(--hero-rgb), var(--hero-end)) 100%); }
.hero::after { content: ""; position: absolute; left: 0; right: 0; bottom: 0; height: 16px; background: radial-gradient(circle at 8px 8px, #fff 0 2px, transparent 2.6px) 0 0/16px 16px, var(--marigold); border-top: 2px solid var(--hero-b); }
.hero-text { position: relative; z-index: 1; }
.hero h1 { font-family: 'Bricolage Grotesque', sans-serif; font-size: clamp(30px, 4vw, 46px); font-weight: 700; line-height: 1.05; color: #fff !important; margin: 0 0 12px 0 !important; padding: 0 !important; text-shadow: 0 2px 14px rgba(0,0,0,.35); }
.hero p { color: rgba(255,255,255,.92) !important; font-size: 1.08rem; max-width: 520px; margin: 0; text-shadow: 0 1px 8px rgba(0,0,0,.35); }
.note { background: var(--marigold-tint); border-left: 5px solid var(--marigold); border-radius: 10px; padding: 12px 16px; color: var(--ink); margin: 0 0 18px 0; }
.hint { color: var(--muted); font-size: .92rem; margin: 2px 0 6px 0; }
.stepper { display: flex; align-items: center; gap: 10px; margin: 6px 0 18px 0; flex-wrap: wrap; }
.step { display: flex; align-items: center; gap: 8px; font-weight: 600; color: var(--muted); }
.step .dot { width: 28px; height: 28px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center; font-size: .85rem; background: var(--dot-bg); color: var(--muted); }
.step.current { color: var(--ink); }
.step.current .dot { background: var(--accent); color: #fff; box-shadow: 0 0 0 3px var(--marigold); }
.step.done .dot { background: var(--dot-done); color: var(--accent-text); }
.stepper .bar { height: 3px; width: 44px; border-radius: 2px; background: var(--track); }
.stepper .bar.done { background: var(--marigold); }
.pill { display: inline-block; padding: 3px 11px; border-radius: 999px; font-size: .82rem; font-weight: 500; background: var(--pill-bg); color: var(--pill-text); margin-right: 6px; }
.pill.high { background: var(--pill-hi-bg); color: var(--pill-hi-text); }
.pill.mid { background: var(--marigold-tint); color: var(--pill-mid-text); }
.pill.low, .pill.flag { background: var(--marigold); color: #2B1B00; }
.item-head { display: flex; align-items: center; flex-wrap: wrap; gap: 6px 0; margin-bottom: 4px; }
.item-name { font-family: 'Bricolage Grotesque', sans-serif; font-weight: 700; font-size: 1.2rem; margin-right: 12px; color: var(--heading); }
.empty { background: var(--tint); border: 2px dashed var(--soft); border-radius: 14px; min-height: 190px; display: flex; flex-direction: column; align-items: center; justify-content: center; color: var(--muted); text-align: center; padding: 18px; }
.empty .icon { font-size: 2.2rem; margin-bottom: 6px; }
.kpis { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin: 8px 0 18px 0; }
.kpi { background: var(--surface); border: 1px solid var(--line); border-top: 5px solid var(--c); border-radius: 14px; padding: 16px 18px; }
.kpi .v { font-family: 'Bricolage Grotesque', sans-serif; font-weight: 700; font-size: 2.1rem; line-height: 1.05; color: var(--ink); }
.kpi .u { font-size: .95rem; color: var(--muted); margin-left: 4px; font-weight: 500; }
.kpi .l { color: var(--muted); margin-top: 8px; font-size: .95rem; }
.kpi.main { background: var(--accent); border-color: var(--accent); border-top-color: var(--marigold); }
.kpi.main .v { color: #fff; }
.kpi.main .u, .kpi.main .l { color: rgba(255,255,255,.88); }
.split { display: flex; height: 14px; border-radius: 7px; overflow: hidden; background: var(--track); }
.legend { display: flex; flex-wrap: wrap; gap: 6px 18px; margin: 10px 0 4px 0; font-size: .92rem; color: var(--ink); }
.legend i { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; }
.target { background: var(--tint); border: 1px solid var(--line); border-radius: 14px; padding: 18px 20px; }
.t-head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 10px; }
.t-head strong { font-family: 'Bricolage Grotesque', sans-serif; font-size: 1.15rem; color: var(--heading); }
.t-head span { font-weight: 600; color: var(--accent-text); }
.t-head span.over { color: var(--over-text); }
.track { height: 12px; border-radius: 6px; background: var(--track); overflow: hidden; }
.fill { height: 100%; border-radius: 6px; background: var(--accent); }
.fill.over { background: var(--marigold); }
.t-sub { margin-top: 10px; color: var(--ink); }
.t-note { color: var(--muted); font-size: .9rem; margin-top: 2px; }
.brand { font-family: 'Bricolage Grotesque', sans-serif; font-weight: 700; font-size: 1.25rem; margin-bottom: 14px; color: var(--heading); }
.profile { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
.avatar { width: 44px; height: 44px; border-radius: 50%; background: var(--accent); color: #fff; box-shadow: 0 0 0 3px var(--marigold); display: flex; align-items: center; justify-content: center; font-family: 'Bricolage Grotesque', sans-serif; font-weight: 700; font-size: 1.2rem; flex-shrink: 0; }
.p-name { font-weight: 600; font-size: 1.05rem; line-height: 1.2; color: var(--ink); }
.p-goal { color: var(--muted); font-size: .9rem; }
.tiles { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 8px; }
.tile { background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 12px 14px; }
.tile .v { font-family: 'Bricolage Grotesque', sans-serif; font-weight: 700; font-size: 1.5rem; line-height: 1.1; color: var(--heading); }
.tile .l { color: var(--muted); font-size: .85rem; margin-top: 2px; }
.ob-track { height: 6px; border-radius: 3px; background: var(--track); margin: 4px 0 6px 0; overflow: hidden; }
.ob-fill { height: 100%; background: var(--accent); border-radius: 3px; }
.dwrap { overflow-x: auto; margin-bottom: 8px; }
.dtable { width: 100%; border-collapse: separate; border-spacing: 0; border: 1px solid var(--line); border-radius: 12px; overflow: hidden; background: var(--surface); }
.dtable th { background: var(--tint); color: var(--heading); text-align: left; font-weight: 600; padding: 10px 14px; }
.dtable td { padding: 10px 14px; border-top: 1px solid var(--line); color: var(--ink); }
.dtable .num { text-align: right; font-variant-numeric: tabular-nums; }
@media (max-width: 760px) { .kpis { grid-template-columns: repeat(2, 1fr); } .hero { padding: 28px 22px 52px 22px; min-height: 180px; } .hero::before { background: linear-gradient(rgba(var(--hero-rgb), .9), rgba(var(--hero-rgb), .72)); } [data-testid="stCheckbox"] { margin-top: 0; } }
"""

CSS_DARK = """
:root {
--ink: #E4F0E9; --muted: #9DB8AA; --line: #2B4A3C; --tint: #122219; --surface: #16291F; --bg: #0B1510; --sidebar: #0F1D16; --field: #0E1B14;
--accent: #2A7F60; --accent-text: #7FD1AA; --heading: #BFE6D2; --soft: #3F6E5A;
--marigold-tint: #33280E; --over-text: #FFC25C;
--track: #243A2F; --dot-bg: #243A2F; --dot-done: #1F4A39;
--pill-bg: #1D3429; --pill-text: #BFE6D2; --pill-hi-bg: #1F4A39; --pill-hi-text: #BFE8D3; --pill-mid-text: #FFD98A;
--hero-a: #17483A; --hero-b: #0A2119; --hero-rgb: 10, 33, 25; --hero-end: .4;
}
html { color-scheme: dark; }
.stApp, [data-testid="stApp"], [data-testid="stAppViewContainer"], [data-testid="stMain"] { background: var(--bg) !important; color: var(--ink); }
[data-testid="stHeader"], [data-testid="stToolbar"] { background: transparent !important; }
[data-testid="stSidebar"], [data-testid="stSidebar"] > div:first-child, [data-testid="stSidebarContent"] { background: var(--sidebar) !important; }
.stApp p, .stApp li, .stApp label, .stApp h1, .stApp h2, .stApp h3, .stApp h4 { color: var(--ink) !important; }
.stApp h2, .stApp h3, .stApp h4 { color: var(--heading) !important; }
.stApp .hero h1 { color: #fff !important; }
.stApp .hero p { color: rgba(255,255,255,.92) !important; }
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] *, [data-testid="stImageCaption"] { color: var(--muted) !important; }
[data-testid="stNumberInputContainer"], [data-testid="stTextInputRootElement"], [data-testid="stSelectbox"] div:has(> input), [data-baseweb="input"], [data-baseweb="base-input"], [data-baseweb="textarea"], [data-baseweb="select"] > div { background: var(--field) !important; border-color: var(--line) !important; }
[data-testid="stSelectbox"] button, [data-testid="stSelectbox"] svg { color: var(--ink) !important; fill: var(--ink) !important; }
.stApp input, .stApp textarea { color: var(--ink) !important; -webkit-text-fill-color: var(--ink) !important; caret-color: var(--ink); background: transparent !important; }
[data-baseweb="select"] div, [data-baseweb="select"] span { color: var(--ink) !important; }
[data-baseweb="select"] svg { fill: var(--ink) !important; }
[data-testid="stNumberInputContainer"] button { background: var(--tint) !important; color: var(--ink) !important; border-color: var(--line) !important; }
[data-baseweb="popover"], [data-baseweb="popover"] > div, [data-baseweb="menu"], ul[role="listbox"] { background: var(--surface) !important; color: var(--ink) !important; }
[role="option"], [data-baseweb="menu"] li { background: var(--surface) !important; color: var(--ink) !important; }
[role="option"]:hover, [role="option"][aria-selected="true"] { background: var(--tint) !important; }
.stApp [data-testid="stBaseButton-secondary"], .stApp [data-testid="stBaseButton-secondaryFormSubmit"] { background: var(--surface) !important; color: var(--ink) !important; border: 1px solid var(--line) !important; }
.stApp [data-testid="stBaseButton-primary"], .stApp [data-testid="stBaseButton-primaryFormSubmit"] { background: var(--accent) !important; border: 1px solid var(--accent) !important; }
.stApp [data-testid="stBaseButton-primary"] p, .stApp [data-testid="stBaseButton-primaryFormSubmit"] p { color: #fff !important; }
.stApp [data-testid="stBaseButton-primary"]:disabled, .stApp [data-testid="stBaseButton-secondary"]:disabled { opacity: .45; }
[data-testid="stExpander"], [data-testid="stExpander"] details { background: var(--surface) !important; border-color: var(--line) !important; }
[data-testid="stExpander"] summary, [data-testid="stExpander"] summary * { color: var(--ink) !important; }
[data-baseweb="tab-border"] { background: var(--line) !important; }
[data-baseweb="tab-highlight"] { background: var(--marigold) !important; }
[data-baseweb="tab"] p { color: var(--muted) !important; }
[data-baseweb="tab"][aria-selected="true"] p { color: var(--ink) !important; }
[data-testid="stFileUploaderDropzone"], [data-testid="stFileUploaderDropzone"] * { color: var(--muted) !important; }
[data-testid="stFileUploaderFile"], [data-testid="stFileUploaderFile"] * { color: var(--ink) !important; }
[data-testid="stVerticalBlock"], [data-testid="stVerticalBlockBorderWrapper"] { border-color: var(--line) !important; }
hr { border-color: var(--line) !important; }
[data-testid="stAlertContainer"] { background: var(--surface) !important; border: 1px solid var(--line); border-radius: 10px; }
[data-testid="stAlert"]:has([data-testid="stAlertContentWarning"]) [data-testid="stAlertContainer"] { background: var(--marigold-tint) !important; border-left: 5px solid var(--marigold); }
[data-testid="stAlert"]:has([data-testid="stAlertContentError"]) [data-testid="stAlertContainer"] { background: #3A1A20 !important; border-left: 5px solid #E5737D; }
[data-testid="stAlert"] p, [data-testid="stAlert"] * { color: var(--ink) !important; }
"""


DEFAULT_HERO_URL = (
    "https://images.unsplash.com/photo-1547592180-85f173990554"
    "?auto=format&fit=crop&w=1600&q=70"
)
HERO_FILENAMES = ("hero.jpg", "hero.jpeg", "hero.png", "hero.webp")


@lru_cache(maxsize=4)
def _embed_photo(path, modified):
    """Resize a local photo and embed it, so it works offline and loads fast."""
    from PIL import Image

    image = Image.open(path).convert("RGB")
    image.thumbnail((1600, 900))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=80)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()


def hero_photo_url():
    """Use assets/hero.jpg (or .png/.webp) if present, else the default photo."""
    for name in HERO_FILENAMES:
        path = ROOT / "assets" / name
        if path.exists():
            try:
                return _embed_photo(str(path), path.stat().st_mtime)
            except Exception:
                continue  # unreadable image: fall back to the default photo
    return DEFAULT_HERO_URL


def inject_css(dark=False):
    css = CSS_BASE.replace("HERO_PHOTO", hero_photo_url())
    if dark:
        css += CSS_DARK
    st.markdown("<style>" + css + "</style>", unsafe_allow_html=True)


def hero_html(title, text):
    return (
        '<div class="hero"><div class="hero-text">'
        f"<h1>{title}</h1><p>{text}</p></div></div>"
    )


def table_html(headers, rows, numeric=()):
    """A small themed HTML table (follows light/dark mode, unlike st.dataframe)."""
    head = "".join(
        f'<th class="{"num" if h in numeric else ""}">{esc(h)}</th>' for h in headers
    )
    body = "".join(
        "<tr>"
        + "".join(
            f'<td class="{"num" if h in numeric else ""}">{esc(str(cell))}</td>'
            for h, cell in zip(headers, row)
        )
        + "</tr>"
        for row in rows
    )
    return (
        '<div class="dwrap"><table class="dtable"><thead><tr>'
        f"{head}</tr></thead><tbody>{body}</tbody></table></div>"
    )


def html_block(markup):
    st.markdown(markup, unsafe_allow_html=True)


def tiles_html(targets):
    return (
        '<div class="tiles">'
        f'<div class="tile"><div class="v">{targets["calories_kcal"]:,}</div>'
        '<div class="l">kcal per day</div></div>'
        f'<div class="tile"><div class="v">{targets["protein_g"]:,} g</div>'
        '<div class="l">protein per day</div></div>'
        "</div>"
    )


def floor_warning():
    st.warning(
        "The calculated calorie estimate was below the app's "
        "1,200 kcal demo floor, so the displayed target was raised "
        "to that floor. This is not a personalized medical recommendation."
    )


def targets_disclaimer():
    st.caption(
        "These are approximate estimates, not measured requirements. "
        "The activity factors and protein estimate may not suit everyone. "
        "Do not rely on this estimator for pregnancy or breastfeeding, "
        "medical conditions, or eating-disorder care without qualified "
        "professional guidance."
    )


# --------------------------------------------------
# 3. First-time onboarding
# --------------------------------------------------

def run_onboarding():
    state = st.session_state
    state.setdefault("onboarding_step", 1)
    state.setdefault("new_profile", {})

    step = state["onboarding_step"]
    draft = state["new_profile"]
    saved = normalize_profile(draft)

    def go(key, value):
        draft[key] = value
        state["onboarding_step"] = step + 1
        st.rerun()

    _, middle, _ = st.columns([1, 3, 1])

    with middle:
        html_block(
            hero_html(
                "Welcome to Nutrition Intelligence",
                "Let's set up your profile. It takes about a minute, "
                "and you only do it once.",
            )
        )
        html_block(
            f'<div class="ob-track"><div class="ob-fill" '
            f'style="width:{step / ONBOARDING_STEPS:.0%}"></div></div>'
        )
        st.caption(f"Step {step} of {ONBOARDING_STEPS}")

        if step == 1:
            st.subheader("First, what should we call you?")
            with st.form("name_form"):
                name = st.text_input(
                    "Your name",
                    value=draft.get("name", ""),
                    placeholder="Enter your name",
                )
                submitted = st.form_submit_button("Continue", type="primary")
            if submitted:
                if name.strip():
                    go("name", name.strip())
                else:
                    st.warning("Please enter your name.")

        elif step == 2:
            st.subheader("How old are you?")
            with st.form("age_form"):
                age = st.number_input(
                    "Your age", min_value=18, max_value=100,
                    value=saved["age"] if "age" in draft else 23, step=1,
                )
                submitted = st.form_submit_button("Continue", type="primary")
            if submitted:
                go("age", int(age))

        elif step == 3:
            st.subheader("How tall are you?")
            default_ft, default_in = ft_in_from_cm(saved["height_cm"])
            with st.form("height_form"):
                col_ft, col_in = st.columns(2)
                with col_ft:
                    height_ft = st.number_input(
                        "Feet", min_value=3, max_value=8,
                        value=default_ft if "height_cm" in draft else 5, step=1,
                    )
                with col_in:
                    height_in = st.number_input(
                        "Extra inches", min_value=0, max_value=11,
                        value=default_in if "height_cm" in draft else 5, step=1,
                    )
                submitted = st.form_submit_button("Continue", type="primary")
            if submitted:
                go("height_cm", (height_ft * 12 + height_in) * 2.54)

        elif step == 4:
            st.subheader("What's your current weight?")
            with st.form("weight_form"):
                weight_kg = st.number_input(
                    "Weight (kg)", min_value=30.0, max_value=300.0,
                    value=saved["weight_kg"], step=0.5,
                )
                submitted = st.form_submit_button("Continue", type="primary")
            if submitted:
                go("weight_kg", float(weight_kg))

        elif step == 5:
            st.subheader("Choose your energy equation setting")
            with st.form("equation_form"):
                equation = st.selectbox(
                    "Energy equation", EQUATIONS,
                    index=EQUATIONS.index(saved["equation"]),
                    help=EQUATION_HELP,
                )
                submitted = st.form_submit_button("Continue", type="primary")
            if submitted:
                go("equation", equation)

        elif step == 6:
            st.subheader("How active are you normally?")
            with st.form("activity_form"):
                activity = st.selectbox(
                    "Your activity level", ACTIVITY_LEVELS,
                    index=ACTIVITY_LEVELS.index(saved["activity"]),
                    help="Choose the level that best describes your usual activity.",
                )
                submitted = st.form_submit_button("Continue", type="primary")
            if submitted:
                go("activity", activity)

        elif step == 7:
            st.subheader("What's your nutrition goal?")
            with st.form("goal_form"):
                goal = st.selectbox(
                    "Choose your goal", GOALS, index=GOALS.index(saved["goal"])
                )
                submitted = st.form_submit_button("Continue", type="primary")
            if submitted:
                go("goal", goal)

        else:
            st.subheader(f"You're all set, {saved['name']}")
            st.write("Here are your estimated daily targets.")
            targets = targets_for(saved)
            html_block(tiles_html(targets))
            if targets["used_calorie_floor"]:
                floor_warning()
            targets_disclaimer()

            if st.button("Save profile and start", type="primary"):
                try:
                    save_user_profile(saved)
                except OSError as exc:
                    st.error(f"Could not save your profile: {exc}")
                else:
                    state.pop("onboarding_step", None)
                    state.pop("new_profile", None)
                    st.rerun()

        if step > 1 and st.button("Back"):
            state["onboarding_step"] = step - 1
            st.rerun()


raw_profile = load_user_profile()

if raw_profile is None:
    inject_css(dark=False)
    run_onboarding()
    st.stop()

profile = normalize_profile(raw_profile)
targets = targets_for(profile)

st.session_state.setdefault("dark_mode_toggle", profile["dark_mode"])
inject_css(dark=bool(st.session_state["dark_mode_toggle"]))


# --------------------------------------------------
# 4. Sidebar: profile summary and editing
# --------------------------------------------------

with st.sidebar:
    html_block('<div class="brand">🥗 Nutrition Intelligence</div>')
    st.toggle("Dark mode", key="dark_mode_toggle", on_change=save_theme_choice)
    html_block(
        '<div class="profile">'
        f'<div class="avatar">{esc(profile["name"][:1].upper())}</div>'
        f'<div><div class="p-name">{esc(profile["name"])}</div>'
        f'<div class="p-goal">{esc(profile["goal"])}</div></div></div>'
    )
    html_block(tiles_html(targets))

    if targets["used_calorie_floor"]:
        floor_warning()

    with st.expander("Edit profile"):
        st.caption("This estimator is intended for adults aged 18 and above.")
        default_ft, default_in = ft_in_from_cm(profile["height_cm"])

        with st.form("nutrition_profile"):
            name = st.text_input("Name", value=profile["name"])
            age = st.number_input(
                "Age (years)", min_value=18, max_value=100, value=profile["age"]
            )
            col_ft, col_in = st.columns(2)
            with col_ft:
                height_ft = st.number_input(
                    "Height (feet)", min_value=3, max_value=8, value=default_ft
                )
            with col_in:
                height_in = st.number_input(
                    "Extra inches", min_value=0, max_value=11, value=default_in
                )
            weight_kg = st.number_input(
                "Weight (kg)", min_value=30.0, max_value=300.0,
                value=profile["weight_kg"], step=0.5,
            )
            equation = st.selectbox(
                "Energy equation setting", EQUATIONS,
                index=EQUATIONS.index(profile["equation"]), help=EQUATION_HELP,
            )
            activity = st.selectbox(
                "Activity level", ACTIVITY_LEVELS,
                index=ACTIVITY_LEVELS.index(profile["activity"]),
                help="Choose the level that best describes your usual activity.",
            )
            goal = st.selectbox("Goal", GOALS, index=GOALS.index(profile["goal"]))
            submitted = st.form_submit_button(
                "Save and update targets", type="primary"
            )

        if submitted:
            updated = normalize_profile(
                {
                    "name": name,
                    "age": int(age),
                    "height_cm": (height_ft * 12 + height_in) * 2.54,
                    "weight_kg": float(weight_kg),
                    "equation": equation,
                    "activity": activity,
                    "goal": goal,
                    "dark_mode": profile["dark_mode"],
                }
            )
            try:
                save_user_profile(updated)
            except OSError as exc:
                st.error(f"Could not save your profile: {exc}")
            else:
                st.rerun()

    with st.expander("Start over"):
        st.caption(
            "Deletes the saved profile and takes you back to first-time setup. "
            "Current meal results are cleared too."
        )
        if st.button("Delete profile and start over"):
            reset_profile()

    targets_disclaimer()

    with st.expander("About this app"):
        st.write("Upload a meal photo to estimate its nutritional content.")
        st.write("Nutrition values come from the project's food-composition database.")
        st.caption("Estimates are not medical advice.")


# --------------------------------------------------
# 5. Header and progress
# --------------------------------------------------

html_block(
    hero_html(
        f"Welcome back, {esc(profile['name'])}!",
        "Upload a meal photo, review the foods we identify, "
        "and understand your nutrition.",
    )
)

stepper_slot = st.empty()  # filled at the end, once the current stage is known

html_block(
    '<div class="note"><strong>Prototype notice:</strong> AI food identification '
    "and portion sizes can be wrong. Review detected foods before "
    "calculating nutrition.</div>"
)


# --------------------------------------------------
# 6. Meal photo upload and analysis
# --------------------------------------------------

col_upload, col_preview = st.columns([3, 2], gap="large")

with col_upload:
    uploaded = st.file_uploader(
        "Upload a meal photo",
        type=["jpg", "jpeg", "png", "webp"],
        label_visibility="collapsed",
    )
    st.caption(
        "JPG, PNG or WebP. Photos with good light and foods that aren't "
        "stacked work best."
    )

    col_analyze, col_manual = st.columns(2)
    with col_analyze:
        analyze_clicked = st.button(
            "Analyze meal", type="primary", disabled=not uploaded
        )
    with col_manual:
        if st.button("Add a food manually"):
            st.session_state["show_manual"] = True

    if analyze_clicked and uploaded:
        with st.spinner("Analyzing your meal..."):
            result = analyze_image(uploaded.getvalue())

        # A new meal starts clean: no old portions, totals or manual foods.
        clear_review_state()
        st.session_state["analysis_result"] = result
        st.session_state["analysis_filename"] = uploaded.name

with col_preview:
    if uploaded:
        st.image(uploaded.getvalue(), caption="Your meal", **STRETCH)
    else:
        html_block(
            '<div class="empty"><div class="icon">🍽️</div>'
            "Your photo will appear here</div>"
        )


# --------------------------------------------------
# 7. Review foods: found by the AI and added by hand
# --------------------------------------------------

result = st.session_state.get("analysis_result")
manual_items = st.session_state.setdefault("manual_items", [])

PROVENANCE = {
    "model_code": "Matched by AI",
    "fuzzy": "Matched by name",
    "none": "No match yet",
}


def confidence_class(confidence):
    if confidence >= 0.8:
        return "high"
    return "mid" if confidence >= 0.5 else "low"


def food_label(name_en, name_bn):
    name_bn = "" if name_bn is None or str(name_bn).lower() == "nan" else str(name_bn).strip()
    return f"{name_en} ({name_bn})" if name_bn else str(name_en)


show_review = (
    result is not None
    or bool(manual_items)
    or st.session_state.get("show_manual", False)
)

if show_review:
    st.divider()
    st.subheader("Review your foods")

    foods_df = load_foods().copy()
    food_codes = foods_df["code"].astype(str).tolist()
    bn_names = (
        foods_df["name_bn_roman"].tolist()
        if "name_bn_roman" in foods_df.columns
        else [None] * len(food_codes)
    )
    food_labels = {
        code: food_label(name, bn)
        for code, name, bn in zip(
            food_codes, foods_df["name_en"].astype(str).tolist(), bn_names
        )
    }

    detected = []

    if result is not None:
        if result.status == "error" and API_KEY_MISSING:
            st.error(
                "Photo analysis isn't set up yet because no Gemini API key was "
                "found. Add GEMINI_API_KEY to the .env file in your project "
                "folder, then restart the app. You can still add foods yourself below."
            )
        elif result.status == "error":
            st.error(result.message or "Unable to analyze this image.")
        elif result.status != "ok":
            st.warning(result.message or "Unable to analyze this image.")
        elif not result.candidates:
            st.warning(
                "No foods were detected. Try another photo, or add foods yourself below."
            )
        else:
            if result.message:
                st.warning(result.message)
            st.write(
                "Review each food and portion. Change the database match, "
                "adjust grams, or remove an item before calculating."
            )
            detected = result.candidates

    edited_items = []

    for i, candidate in enumerate(detected):
        with st.container(border=True):
            matched = candidate.fct_code in food_labels
            flag = (
                '<span class="pill flag">Please confirm food and portion</span>'
                if candidate.needs_confirmation
                else ""
            )
            dish = (
                f'<span class="pill">Part of {esc(str(candidate.dish))}</span>'
                if getattr(candidate, "dish", "")
                else ""
            )
            html_block(
                '<div class="item-head">'
                f'<span class="item-name">{esc(str(candidate.detected_name))}</span>'
                f'<span class="pill {confidence_class(candidate.confidence)}">'
                f"AI confidence {candidate.confidence:.0%}</span>"
                f'<span class="pill">{PROVENANCE.get(candidate.match_method, "Match")}</span>'
                f"{dish}{flag}</div>"
            )

            if not matched and getattr(candidate, "alternatives", None):
                names = ", ".join(esc(a["name_en"]) for a in candidate.alternatives)
                html_block(f'<div class="hint">Closest matches: {names}</div>')

            col_food, col_grams, col_include = st.columns([3, 2, 1.3])

            with col_food:
                selected_code = st.selectbox(
                    "Food database match",
                    options=food_codes,
                    index=food_codes.index(candidate.fct_code) if matched else None,
                    placeholder="Pick the closest food",
                    format_func=lambda code: food_labels[code],
                    key=f"food_{i}",
                )

            with col_grams:
                grams = st.number_input(
                    "Estimated edible portion (grams)",
                    min_value=1.0,
                    max_value=5000.0,
                    value=max(1.0, min(5000.0, float(candidate.grams))),
                    step=5.0,
                    key=f"grams_{i}",
                )

            with col_include:
                include = st.checkbox(
                    "Include this item", value=True, key=f"include_{i}"
                )

            if include and selected_code is None:
                st.caption("Pick a food from the database to include this item.")
            elif include:
                edited_items.append(MealItem(fct_code=selected_code, grams=grams))

    for item in list(manual_items):
        item_id = item["id"]
        with st.container(border=True):
            html_block(
                '<div class="item-head">'
                f'<span class="item-name">{esc(food_labels[item["code"]])}</span>'
                '<span class="pill">Added by you</span></div>'
            )
            col_grams, col_include, col_remove = st.columns([3, 1.6, 1])

            with col_grams:
                grams = st.number_input(
                    "Portion (grams)",
                    min_value=1.0,
                    max_value=5000.0,
                    value=float(item["grams"]),
                    step=5.0,
                    key=f"mgrams_{item_id}",
                )

            with col_include:
                include = st.checkbox(
                    "Include this item", value=True, key=f"minc_{item_id}"
                )

            with col_remove:
                st.markdown('<div style="height:1.75rem"></div>', unsafe_allow_html=True)
                if st.button("Remove", key=f"mrm_{item_id}"):
                    manual_items[:] = [m for m in manual_items if m["id"] != item_id]
                    st.rerun()

            if include:
                edited_items.append(MealItem(fct_code=item["code"], grams=grams))

    with st.form("manual_add_form", clear_on_submit=True):
        html_block(
            '<div class="item-head"><span class="item-name">Add a food manually</span></div>'
        )
        st.caption(
            "Missing something, or the photo analysis isn't available? "
            "Search by English or Bangla name and enter the portion in grams."
        )
        col_food, col_grams = st.columns([3, 1.4])
        with col_food:
            new_code = st.selectbox(
                "Food",
                options=food_codes,
                index=None,
                placeholder="Search foods, for example rice or dal",
                format_func=lambda code: food_labels[code],
                key="manual_food",
            )
        with col_grams:
            new_grams = st.number_input(
                "Portion (grams)",
                min_value=1.0,
                max_value=5000.0,
                value=100.0,
                step=5.0,
                key="manual_grams",
            )
        added = st.form_submit_button("Add food", type="primary")

    if added:
        if new_code is None:
            st.warning("Choose a food to add.")
        else:
            seq = st.session_state.get("manual_seq", 0) + 1
            st.session_state["manual_seq"] = seq
            manual_items.append(
                {"id": seq, "code": new_code, "grams": float(new_grams)}
            )
            st.rerun()

    if detected or manual_items:
        if st.button("Calculate nutrition", type="primary"):
            if not edited_items:
                st.warning("Include at least one food item.")
            else:
                try:
                    st.session_state["meal_totals"] = compute_meal(edited_items)
                except Exception as exc:
                    st.error(f"Could not calculate nutrition: {exc}")


# --------------------------------------------------
# 8. Nutrition report and daily target comparison
# --------------------------------------------------

def target_block(title, value, target, unit, decimals):
    """One daily-target comparison card, as HTML."""
    if value is None or target <= 0:
        return (
            f'<div class="target"><div class="t-head"><strong>{title}</strong></div>'
            f'<div class="t-sub">{title} comparison unavailable.</div></div>'
        )

    fraction = value / target
    over = value > target
    cls = " over" if over else ""
    diff = abs(target - value)
    note = (
        f"Meal exceeds the daily estimate by {diff:,.{decimals}f} {unit}."
        if over
        else f"Difference from target: {diff:,.{decimals}f} {unit}"
    )

    return (
        f'<div class="target"><div class="t-head"><strong>{title}</strong>'
        f'<span class="{cls.strip()}">{fraction:.0%} of daily target</span></div>'
        f'<div class="track"><div class="fill{cls}" '
        f'style="width:{min(1.0, fraction) * 100:.1f}%"></div></div>'
        f'<div class="t-sub">{value:,.{decimals}f} of {target:,} {unit}</div>'
        f'<div class="t-note">{note}</div></div>'
    )


if "meal_totals" in st.session_state:
    totals = st.session_state["meal_totals"]
    values = totals.totals

    st.divider()
    st.subheader("Estimated nutrition")

    summary_metrics = [
        ("Energy", "energy_kcal", "kcal", "var(--marigold)", 0),
        ("Protein", "protein_g", "g", "var(--accent)", 1),
        ("Carbohydrates", "carb_g", "g", "var(--marigold)", 1),
        ("Fat", "fat_g", "g", "var(--fat)", 1),
    ]

    cards = []
    for label, key, unit, color, decimals in summary_metrics:
        value = values.get(key)
        shown = (
            f'{value:,.{decimals}f}<span class="u">{unit}</span>'
            if value is not None
            else '<span class="u">Unavailable</span>'
        )
        cards.append(
            f'<div class="kpi{" main" if key == "energy_kcal" else ""}" '
            f'style="--c:{color}"><div class="v">{shown}</div>'
            f'<div class="l">{label}</div></div>'
        )
    html_block(f'<div class="kpis">{"".join(cards)}</div>')

    protein_g, carb_g, fat_g = (values.get(k) for k in ("protein_g", "carb_g", "fat_g"))
    if None not in (protein_g, carb_g, fat_g):
        parts = [
            ("Protein", 4 * protein_g, "var(--accent)"),
            ("Carbohydrates", 4 * carb_g, "var(--marigold)"),
            ("Fat", 9 * fat_g, "var(--fat)"),
        ]
        macro_kcal = sum(p[1] for p in parts)
        if macro_kcal > 0:
            bars = "".join(
                f'<div style="width:{kcal / macro_kcal * 100:.1f}%;background:{color}"></div>'
                for _, kcal, color in parts
            )
            legend = "".join(
                f'<span><i style="background:{color}"></i>{name} {kcal / macro_kcal:.0%}</span>'
                for name, kcal, color in parts
            )
            st.markdown("**Where the calories come from**")
            html_block(f'<div class="split">{bars}</div><div class="legend">{legend}</div>')
            st.caption("Share of calories from protein, carbohydrates and fat in this meal.")

    st.markdown("&nbsp;", unsafe_allow_html=True)
    st.subheader("Your daily target comparison")
    st.caption(
        "This compares one meal with your estimated daily targets. "
        "It is not a recommendation to consume the remaining amount "
        "in one sitting."
    )

    col_cal, col_pro = st.columns(2, gap="large")
    with col_cal:
        html_block(
            target_block(
                "Calories", values.get("energy_kcal"),
                targets["calories_kcal"], "kcal", 0,
            )
        )
    with col_pro:
        html_block(
            target_block(
                "Protein", values.get("protein_g"),
                targets["protein_g"], "g", 1,
            )
        )

    st.markdown("&nbsp;", unsafe_allow_html=True)

    nutrient_labels = {
        "energy_kcal": "Energy (kcal)",
        "protein_g": "Protein (g)",
        "fat_g": "Fat (g)",
        "carb_g": "Carbohydrates (g)",
        "fiber_g": "Fiber (g)",
        "sodium_mg": "Sodium (mg)",
        "calcium_mg": "Calcium (mg)",
        "iron_mg": "Iron (mg)",
        "vitamin_a_rae_mcg": "Vitamin A (µg RAE)",
        "vitamin_c_mg": "Vitamin C (mg)",
        "folate_mcg": "Folate (µg)",
    }

    tab_nutrients, tab_foods = st.tabs(["Nutrient breakdown", "Foods used"])

    def fmt_grams(value):
        return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.1f}"

    with tab_nutrients:
        html_block(
            table_html(
                ["Nutrient", "Amount"],
                [
                    (
                        label,
                        f"{values[key]:,.2f}"
                        if values.get(key) is not None
                        else "Unavailable",
                    )
                    for key, label in nutrient_labels.items()
                ],
                numeric=("Amount",),
            )
        )

        if totals.unknown_by_nutrient:
            st.warning(
                "Some nutrient values are missing from the database. "
                "Totals for those nutrients may be incomplete."
            )

    with tab_foods:
        html_block(
            table_html(
                ["Food", "Portion (g)", "Edible portion (g)"],
                [
                    (
                        line.name_en,
                        fmt_grams(line.grams_input),
                        fmt_grams(line.edible_grams),
                    )
                    for line in totals.lines
                ],
                numeric=("Portion (g)", "Edible portion (g)"),
            )
        )

    st.caption(
        "Results depend on food identification, portion size, preparation "
        "method, and database coverage. They are estimates, not a diagnosis "
        "or a substitute for professional dietary advice."
    )


# --------------------------------------------------
# 9. Progress indicator (needs the final state of this run)
# --------------------------------------------------

STAGES = ["Upload photo", "Review foods", "See nutrition"]

if "meal_totals" in st.session_state:
    stage = 3
elif st.session_state.get("analysis_result") is not None:
    stage = 2
else:
    stage = 1

steps_html = []
for n, label in enumerate(STAGES, start=1):
    state_name = "done" if n < stage else "current" if n == stage else "todo"
    mark = "✓" if state_name == "done" else str(n)
    current = ' aria-current="step"' if state_name == "current" else ""
    steps_html.append(
        f'<div class="step {state_name}"{current}><span class="dot">{mark}</span>'
        f"<span>{label}</span></div>"
    )
    if n < len(STAGES):
        steps_html.append(f'<div class="bar{" done" if n < stage else ""}"></div>')

stepper_slot.markdown(
    f'<div class="stepper">{"".join(steps_html)}</div>', unsafe_allow_html=True
)
