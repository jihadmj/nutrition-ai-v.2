import json
import sys
from pathlib import Path


import streamlit as st

ROOT = Path(__file__).resolve().parent

# --------------------------------------------------

# User profile storage

# --------------------------------------------------


PROFILE_FILE = ROOT / "user_profile.json"


def load_user_profile():

    """Load the saved user profile, if one exists."""

    if not PROFILE_FILE.exists():

        return None


    try:


        with PROFILE_FILE.open("r", encoding="utf-8") as file:

            return json.load(file)


    except (json.JSONDecodeError, OSError):

        return None


def save_user_profile(profile):

    """Save the user profile locally as JSON."""


    with PROFILE_FILE.open("w", encoding="utf-8") as file:

        json.dump(profile, file, indent=4)

from backend.app.perception.pipeline import analyze_image

from backend.app.nutrition.foods import load_foods

from backend.app.nutrition.engine import compute_meal

from backend.app.schemas import MealItem


# --------------------------------------------------

# 1. Personalized daily target estimation

# --------------------------------------------------


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


    estimated_calories = round(

        maintenance + goal_adjustments[goal]

    )


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


# --------------------------------------------------

# Load saved user profile

# --------------------------------------------------


user_profile = load_user_profile()

if user_profile is not None and "daily_targets" not in st.session_state:
    st.session_state["daily_targets"] = estimate_daily_targets(
        int(user_profile["age"]),
        float(user_profile["height_cm"]),
        float(user_profile["weight_kg"]),
        user_profile["equation"],
        user_profile["activity"],
        user_profile["goal"],
    )

# --------------------------------------------------

# 2. Page setup

# --------------------------------------------------


st.set_page_config(

    page_title="Nutrition Intelligence",

    page_icon="🥗",

    layout="wide",

)

# --------------------------------------------------

# First-time user onboarding

# --------------------------------------------------


if user_profile is None:


    if "onboarding_step" not in st.session_state:

        st.session_state["onboarding_step"] = 1


    if "new_profile" not in st.session_state:

        st.session_state["new_profile"] = {}


    st.title("🥗 Welcome to Nutrition Intelligence")

    st.write("Let's set up your nutrition profile.")


    step = st.session_state["onboarding_step"]


    # STEP 1: Name

    if step == 1:

        st.subheader("First, what should we call you?")


        with st.form("name_form"):

            name = st.text_input(

                "Your name",

                placeholder="Enter your name",

            )


            submitted = st.form_submit_button(

                "Continue",

                type="primary",

            )


        if submitted:

            if name.strip():

                st.session_state["new_profile"]["name"] = name.strip()

                st.session_state["onboarding_step"] = 2

                st.rerun()

            else:

                st.warning("Please enter your name.")


    # STEP 2: Age

    elif step == 2:

        st.subheader("How old are you?")


        with st.form("age_form"):

            age = st.number_input(

                "Your age",

                min_value=18,

                max_value=100,

                value=23,

                step=1,

            )


            submitted = st.form_submit_button(

                "Continue",

                type="primary",

            )


        if submitted:

            st.session_state["new_profile"]["age"] = int(age)

            st.session_state["onboarding_step"] = 3

            st.rerun()

        # STEP 3: Height

    elif step == 3:

        st.subheader("How tall are you?")


        with st.form("height_form"):

            col_ft, col_in = st.columns(2)


            with col_ft:

                height_ft = st.number_input(

                    "Feet",

                    min_value=3,

                    max_value=8,

                    value=(int((user_profile["height_cm"] / 2.54) // 12) if user_profile else 5),

                    step=1,

                )


            with col_in:

                height_in = st.number_input(

                    "Extra inches",

                    min_value=0,

                    max_value=11,

                    value=(round(user_profile["height_cm"] / 2.54) % 12 if user_profile else 5),

                    step=1,

                )


            submitted = st.form_submit_button(

                "Continue",

                type="primary",

            )


        if submitted:

            height_cm = (height_ft * 12 + height_in) * 2.54


            st.session_state["new_profile"]["height_cm"] = height_cm

            st.session_state["onboarding_step"] = 4

            st.rerun()

        # STEP 4: Weight

    elif step == 4:

        st.subheader("What's your current weight?")


        with st.form("weight_form"):

            weight_kg = st.number_input(

                "Weight (kg)",

                min_value=30.0,

                max_value=300.0,

                value=float(user_profile["weight_kg"]) if user_profile else 65.0,

                step=0.5,

            )


            submitted = st.form_submit_button(

                "Continue",

                type="primary",

            )


        if submitted:

            st.session_state["new_profile"]["weight_kg"] = float(weight_kg)

            st.session_state["onboarding_step"] = 5

            st.rerun()

        # STEP 5: Energy equation

    elif step == 5:

        st.subheader("Choose your energy equation setting")


        with st.form("equation_form"):

            equation = st.selectbox(

                "Energy equation",

                ["Female equation", "Male equation"],

                help=(

                    "Choose the equation parameter that best fits you. "

                    "This is a calculation setting, not a gender identity label."

                ),

            )


            submitted = st.form_submit_button(

                "Continue",

                type="primary",

            )


        if submitted:

            st.session_state["new_profile"]["equation"] = equation

            st.session_state["onboarding_step"] = 6

            st.rerun()

        # STEP 6: Activity level

    elif step == 6:

        st.subheader("How active are you normally?")


        with st.form("activity_form"):

            activity = st.selectbox(

                "Your activity level",

                [

                    "Sedentary",

                    "Lightly active",

                    "Moderately active",

                    "Very active",

                    "Extra active",

                ],

                help="Choose the level that best describes your usual activity.",

            )


            submitted = st.form_submit_button(

                "Continue",

                type="primary",

            )


        if submitted:

            st.session_state["new_profile"]["activity"] = activity

            st.session_state["onboarding_step"] = 7

            st.rerun()        

        # STEP 6: Activity level

         # STEP 7: Nutrition goal

    elif step == 7:

        st.subheader("What's your nutrition goal?")


        with st.form("goal_form"):

            goal = st.selectbox(

                "Choose your goal",

                [

                    "Maintain weight",

                    "Gradual weight loss",

                    "Gradual muscle gain",

                ],

            )


            submitted = st.form_submit_button(

                "Continue",

                type="primary",

            )


        if submitted:

            st.session_state["new_profile"]["goal"] = goal

            st.session_state["onboarding_step"] = 8

            st.rerun()        


    elif step == 8:
        st.subheader("Review your nutrition profile")
        profile = st.session_state["new_profile"]
        st.write(f"**Name:** {profile['name']}")
        st.write(f"**Age:** {profile['age']} years")
        st.write(f"**Height:** {profile['height_cm']:.1f} cm")
        st.write(f"**Weight:** {profile['weight_kg']:.1f} kg")
        st.write(f"**Energy equation:** {profile['equation']}")
        st.write(f"**Activity:** {profile['activity']}")
        st.write(f"**Goal:** {profile['goal']}")

        back_col, save_col = st.columns(2)
        with back_col:
            if st.button("Back", use_container_width=True):
                st.session_state["onboarding_step"] = 7
                st.rerun()
        with save_col:
            if st.button("Save profile", type="primary", use_container_width=True):
                try:
                    save_user_profile(profile)
                    st.session_state.pop("new_profile", None)
                    st.session_state.pop("onboarding_step", None)
                    st.rerun()
                except OSError as exc:
                    st.error(f"Could not save your profile: {exc}")

    st.stop()

st.markdown(

    f"""

    <style>

    .welcome-banner {

        background:

            linear-gradient(90deg, rgba(20, 24, 29, 0.88), rgba(20, 24, 29, 0.20)),

            url("https://images.unsplash.com/photo-1547592180-85f173990554");

        background-size: cover;

        background-position: center;

        border-radius: 10px;

        padding: 54px 32px;

        color: white;

        margin-bottom: 22px;

    }


    .welcome-banner h1 {

        color: white;

        font-size: clamp(28px, 3vw, 42px);

        font-weight: 650;

        margin: 0 0 12px 0;

    }


    .welcome-banner p {

        color: white;

        font-size: 17px;

        margin: 0;

        max-width: 650px;

    }

    </style>


    <div class="welcome-banner">

        <h1>Welcome back, {user_profile['name']}!</h1>

        <p>

            Upload a meal photo, review the foods we identify,

            and understand your nutrition.

        </p>

    </div>

    """,

    unsafe_allow_html=True,

)

st.info(

    "Prototype notice: AI food identification and portion sizes can be wrong. "

    "Review detected foods before calculating nutrition."

)


# --------------------------------------------------

# 3. Sidebar and nutrition profile

# --------------------------------------------------


with st.sidebar:

    st.header("About")

    st.write("Upload a meal photo to estimate its nutritional content.")

    st.write("Nutrition values come from the project's food-composition database.")

    st.caption("Estimates are not medical advice.")


    st.divider()

    st.subheader("Your nutrition profile")

    st.caption("This estimator is intended for adults aged 18 and above.")


    with st.form("nutrition_profile"):

        age = st.number_input(

            "Age (years)",

            min_value=18,

            max_value=100,

            value=int(user_profile["age"]) if user_profile else 25,

        )


        col_ft, col_in = st.columns(2)


        with col_ft:

            height_ft = st.number_input(

                "Height (feet)",

                min_value=3,

                max_value=8,

                value=5,

            )


        with col_in:

            height_in = st.number_input(

                "Extra inches",

                min_value=0,

                max_value=11,

                value=5,

            )


        weight_kg = st.number_input(

            "Weight (kg)",

            min_value=30.0,

            max_value=300.0,

            value=65.0,

            step=0.5,

        )


        equation = st.selectbox(

            "Energy equation setting",

            ["Female equation", "Male equation"],

            help=(

                "Choose the equation parameter that best fits you. "

                "This is a calculation setting, not a gender identity label."

            ),

        )


        activity = st.selectbox(

            "Activity level",

            [

                "Sedentary",

                "Lightly active",

                "Moderately active",

                "Very active",

                "Extra active",

            ],

            help="Choose the level that best describes your usual activity.",

        )


        goal = st.selectbox(

            "Goal",

            [

                "Maintain weight",

                "Gradual weight loss",

                "Gradual muscle gain",

            ],

        )


        submitted = st.form_submit_button(

            "Estimate daily targets",

            type="primary",

        )


    if submitted:

        # Convert feet and inches into centimetres for the equation.

        height_cm = (height_ft * 12 + height_in) * 2.54


        st.session_state["daily_targets"] = estimate_daily_targets(

            int(age),

            height_cm,

            float(weight_kg),

            equation,

            activity,

            goal,

        )

        st.session_state["profile_goal"] = goal
        if user_profile is not None:
            updated_profile = {
                "name": user_profile["name"],
                "age": int(age),
                "height_cm": float(height_cm),
                "weight_kg": float(weight_kg),
                "equation": equation,
                "activity": activity,
                "goal": goal,
            }
            try:
                save_user_profile(updated_profile)
                user_profile = updated_profile
            except OSError as exc:
                st.error(f"Could not update your saved profile: {exc}")


    if "daily_targets" in st.session_state:

        targets = st.session_state["daily_targets"]


        st.success("Daily targets estimated")

        st.metric(

            "Estimated daily calories",

            f'{targets["calories_kcal"]:,} kcal',

        )

        st.metric(

            "Estimated daily protein",

            f'{targets["protein_g"]:,} g',

        )


        if targets["used_calorie_floor"]:

            st.warning(

                "The calculated calorie estimate was below the app's "

                "1,200 kcal demo floor, so the displayed target was raised "

                "to that floor. This is not a personalized medical recommendation."

            )


        st.caption(

            "These are approximate estimates, not measured requirements. "

            "The activity factors and protein estimate may not suit everyone. "

            "Do not rely on this estimator for pregnancy or breastfeeding, "

            "medical conditions, or eating-disorder care without qualified "

            "professional guidance."

        )


# --------------------------------------------------

# 4. Meal photo upload and analysis

# --------------------------------------------------


uploaded = st.file_uploader(

    "Upload a meal photo",

    type=["jpg", "jpeg", "png", "webp"],

)


if uploaded:

    image_bytes = uploaded.getvalue()

    st.image(

        image_bytes,

        caption="Your meal",

        use_container_width=True,

    )


    if st.button("Analyze meal", type="primary"):

        with st.spinner("Analyzing your meal..."):

            result = analyze_image(image_bytes)


        st.session_state["analysis_result"] = result

        st.session_state["analysis_filename"] = uploaded.name


        # Remove results from the previous meal.

        st.session_state.pop("meal_totals", None)


# --------------------------------------------------

# 5. Review and edit detected foods

# --------------------------------------------------


result = st.session_state.get("analysis_result")


if result is not None:

    st.divider()

    st.subheader("1. Review detected foods")


    if result.status != "ok":

        st.error(result.message or "Unable to analyze this image.")


    elif not result.candidates:

        st.warning("No foods were detected. Try another photo.")


    else:

        if result.message:

            st.warning(result.message)


        st.write(

            "Review each food and portion. Change the database match, "

            "adjust grams, or remove an item before calculating."

        )


        foods_df = load_foods().copy()


        food_codes = foods_df["code"].astype(str).tolist()

        food_names = foods_df["name_en"].astype(str).tolist()

        code_to_name = dict(zip(food_codes, food_names))


        edited_items = []


        for i, candidate in enumerate(result.candidates):

            with st.container(border=True):

                st.markdown(

                    f"**Item {i + 1}: {candidate.detected_name}**"

                )


                st.caption(

                    f"AI confidence: {candidate.confidence:.0%} · "

                    f"Match method: {candidate.match_method}"

                )


                if candidate.needs_confirmation:

                    st.warning(

                        "Please confirm this food match and its portion."

                    )


                if candidate.fct_code in code_to_name:

                    default_code = candidate.fct_code

                else:

                    default_code = food_codes[0]


                selected_code = st.selectbox(

                    "Food database match",

                    options=food_codes,

                    index=food_codes.index(default_code),

                    format_func=lambda code: code_to_name[code],

                    key=f"food_{i}",

                )


                grams = st.number_input(

                    "Estimated edible portion (grams)",

                    min_value=1.0,

                    max_value=5000.0,

                    value=max(1.0, min(5000.0, float(candidate.grams))),

                    step=5.0,

                    key=f"grams_{i}",

                )


                include = st.checkbox(

                    "Include this item",

                    value=True,

                    key=f"include_{i}",

                )


                if include:

                    edited_items.append(

                        MealItem(

                            fct_code=selected_code,

                            grams=grams,

                        )

                    )


        if st.button("Calculate nutrition", type="primary"):

            if not edited_items:

                st.warning("Include at least one food item.")


            else:

                try:

                    totals = compute_meal(edited_items)

                    st.session_state["meal_totals"] = totals


                except Exception as exc:

                    st.error(f"Could not calculate nutrition: {exc}")


# --------------------------------------------------

# 6. Nutrition report and daily target comparison

# --------------------------------------------------


if "meal_totals" in st.session_state:

    totals = st.session_state["meal_totals"]

    values = totals.totals


    st.divider()

    st.subheader("2. Estimated nutrition")


    summary_metrics = [

        ("Energy", "energy_kcal", "kcal"),

        ("Protein", "protein_g", "g"),

        ("Carbohydrates", "carb_g", "g"),

        ("Fat", "fat_g", "g"),

    ]


    columns = st.columns(4)


    for column, (label, key, unit) in zip(columns, summary_metrics):

        value = values.get(key)


        if value is not None:

            column.metric(label, f"{value:,.1f} {unit}")

        else:

            column.metric(label, "Unavailable")


    # Compare this meal against the user's saved daily estimates.

    if "daily_targets" in st.session_state:

        targets = st.session_state["daily_targets"]


        st.divider()

        st.subheader("3. Your daily target comparison")

        st.caption(

            "This compares one meal with your estimated daily targets. "

            "It is not a recommendation to consume the remaining amount "

            "in one sitting."

        )


        calorie_target = targets["calories_kcal"]

        protein_target = targets["protein_g"]


        calorie_value = values.get("energy_kcal")

        protein_value = values.get("protein_g")


        col_cal, col_pro = st.columns(2)


        with col_cal:

            st.markdown("**Calories**")


            if calorie_value is not None and calorie_target > 0:

                calorie_fraction = calorie_value / calorie_target


                st.progress(min(1.0, calorie_fraction))

                st.write(

                    f"{calorie_value:,.0f} of "

                    f"{calorie_target:,} kcal"

                )

                st.write(

                    f"{calorie_fraction:.0%} of estimated daily target"

                )


                if calorie_value <= calorie_target:

                    st.caption(

                        f"Difference from target: "

                        f"{calorie_target - calorie_value:,.0f} kcal"

                    )

                else:

                    st.caption(

                        f"Meal exceeds the daily estimate by "

                        f"{calorie_value - calorie_target:,.0f} kcal."

                    )

            else:

                st.write("Calorie comparison unavailable.")


        with col_pro:

            st.markdown("**Protein**")


            if protein_value is not None and protein_target > 0:

                protein_fraction = protein_value / protein_target


                st.progress(min(1.0, protein_fraction))

                st.write(

                    f"{protein_value:,.1f} of {protein_target:,} g"

                )

                st.write(

                    f"{protein_fraction:.0%} of estimated daily target"

                )


                if protein_value <= protein_target:

                    st.caption(

                        f"Difference from target: "

                        f"{protein_target - protein_value:,.1f} g"

                    )

                else:

                    st.caption(

                        f"Meal exceeds the daily estimate by "

                        f"{protein_value - protein_target:,.1f} g."

                    )

            else:

                st.write("Protein comparison unavailable.")


    # Full nutrient breakdown.

    st.divider()

    st.markdown("#### Nutrient breakdown")


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


    table_rows = []


    for key, label in nutrient_labels.items():

        value = values.get(key)


        table_rows.append(

            {

                "Nutrient": label,

                "Amount": (

                    f"{value:,.2f}"

                    if value is not None

                    else "Unavailable"

                ),

            }

        )


    st.dataframe(

        table_rows,

        hide_index=True,

        use_container_width=True,

    )


    if totals.unknown_by_nutrient:

        st.warning(

            "Some nutrient values are missing from the database. "

            "Totals for those nutrients may be incomplete."

        )


    st.markdown("#### Foods used in the calculation")


    st.dataframe(

        [

            {

                "Food": line.name_en,

                "Portion (g)": line.grams_input,

                "Edible portion (g)": line.edible_grams,

            }

            for line in totals.lines

        ],

        hide_index=True,

        use_container_width=True,

    )


    st.caption(

        "Results depend on food identification, portion size, preparation "

        "method, and database coverage. They are estimates, not a diagnosis "

        "or a substitute for professional dietary advice."

    )
