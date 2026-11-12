import logging

from ..nutrition.foods import get_food, load_foods, search_foods
from .image import prepare_image
from .models import AnalysisResult, Candidate
from .vlm import detect_foods, explain_failure

logger = logging.getLogger(__name__)

CONFIRM_BELOW = 0.6
FUZZY_MIN = 0.75
MIN_ALT_SCORE = 0.45
DEFAULT_GRAMS = 100.0
MAX_GRAMS = 1500.0


def _clean_grams(g: float | None) -> tuple[float, bool]:
    if g is None or g <= 0:
        return DEFAULT_GRAMS, True
    return min(max(float(g), 5.0), MAX_GRAMS), False


def analyze_image(raw: bytes) -> AnalysisResult:
    try:
        prepared = prepare_image(raw)
    except Exception:
        return AnalysisResult(status="error", message="Could not read this image. Please try another photo.")

    try:
        vision = detect_foods(prepared)
    except Exception as exc:
        logger.exception("Vision request failed")  # full details in the terminal
        return AnalysisResult(
            status="error",
            message=f"{explain_failure(exc)} You can add foods manually.",
        )

    if vision.scene_type == "menu":
        return AnalysisResult(
            status="unsupported",
            message="Menu reading is not enabled yet. Please photograph ingredients or a plate.",
        )
    if vision.scene_type in ("unusable", "other") or not vision.items:
        msg = vision.quality_issue or "No food detected. Retake the photo in good light with the food in frame."
        return AnalysisResult(status="needs_retake", message=msg)

    valid = set(load_foods().index)
    candidates: list[Candidate] = []
    for it in vision.items:
        grams, defaulted = _clean_grams(it.estimated_grams)
        conf = max(0.0, min(1.0, float(it.confidence)))
        query = it.name_en or it.name_bn_roman
        hits = search_foods(query, limit=5) if query else []

        code, method = None, "none"
        if it.fct_code in valid:
            code, method = it.fct_code, "model_code"
        elif hits and hits[0]["score"] >= FUZZY_MIN:
            code, method = hits[0]["code"], "fuzzy"

        alts = [h for h in hits if h["code"] != code and h["score"] >= MIN_ALT_SCORE][:3]
        candidates.append(
            Candidate(
                fct_code=code,
                matched_name=str(get_food(code)["name_en"]) if code else None,
                detected_name=it.name_en,
                dish=it.dish,
                grams=round(grams, 1),
                confidence=round(conf, 2),
                match_method=method,
                needs_confirmation=(conf < CONFIRM_BELOW) or method != "model_code" or defaulted or bool(it.dish),
                alternatives=[{"code": h["code"], "name_en": h["name_en"]} for h in alts],
            )
        )

        # A prepared-dish entry (marked * in the table) already includes its cooking fat.
    recipe_dishes = {
        c.dish for c in candidates
        if c.dish and c.fct_code and bool(get_food(c.fct_code)["is_recipe_calc"])
    }
    if recipe_dishes:
        candidates = [
            c for c in candidates
            if not (c.dish in recipe_dishes and c.fct_code
                    and str(get_food(c.fct_code)["food_group_code"]) == "13")
        ]

    unmatched = sum(1 for c in candidates if c.fct_code is None)
    msg = f"{unmatched} of {len(candidates)} items need your confirmation." if unmatched else ""
    return AnalysisResult(status="ok", message=msg, candidates=candidates)