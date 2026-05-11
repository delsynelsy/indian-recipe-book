"""
Parsers for recipes.yaml and meal_plan.md.
"""

import re
from pathlib import Path

import yaml

from .models import MealPlanProfile, NutritionInfo, Recipe, RecipeImage, Step


# ── YAML recipe loader ────────────────────────────────────────────

def load_recipes(yaml_path: Path) -> list:
    with open(yaml_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return [_parse_recipe(r) for r in data["recipes"]]


def _parse_recipe(d: dict) -> Recipe:
    nutrition = NutritionInfo(
        kcal=d["nutrition"]["kcal"],
        protein=d["nutrition"]["protein"],
        carbs=d["nutrition"]["carbs"],
        fat=d["nutrition"]["fat"],
    )
    image = RecipeImage(
        src=d["image"]["src"],
        css_class=d["image"]["css_class"],
    )
    steps = [
        Step(
            icon=s["icon"],
            action=s["action"],
            text=s["text"],
            time=s.get("time", ""),
        )
        for s in d.get("steps", [])
    ]
    highlight = d.get("highlight_ingredients", d.get("ingredients", [])[:5])
    return Recipe(
        id=d["id"],
        name=d["name"],
        subtitle=d["subtitle"],
        phase=d["phase"],
        meal=d["meal"],
        type=d["type"],
        prep=d["prep"],
        prep_note=d.get("prep_note", ""),
        cook=d["cook"],
        servings=d["servings"],
        nutrition=nutrition,
        image=image,
        ingredients=d.get("ingredients", []),
        steps=steps,
        tip=d.get("tip", ""),
        highlight_ingredients=highlight,
    )


# ── Markdown meal plan parser ─────────────────────────────────────

def parse_meal_plan(md_path: Path) -> MealPlanProfile:
    text = md_path.read_text(encoding="utf-8")

    def extract_float(pattern: str, default: float = 0.0) -> float:
        m = re.search(pattern, text)
        return float(m.group(1).replace(",", "")) if m else default

    def extract_int(pattern: str, default: int = 0) -> int:
        return int(extract_float(pattern, default))

    return MealPlanProfile(
        current_weight=extract_float(r"Current Weight\s*\|\s*([\d.]+)"),
        goal_weight=extract_float(r"Goal Weight\s*\|\s*([\d.]+)"),
        height=extract_float(r"Height\s*\|\s*([\d.]+)"),
        age=extract_int(r"Age\s*\|\s*(\d+)"),
        bmr=extract_float(r"BMR[^\d]*([\d,]+)"),
        tdee_min=extract_float(r"TDEE[^\d]*([\d,]+)"),
        tdee_max=extract_float(r"TDEE[^–\d]*([\d,]+)[^\d]*([\d,]+)", 1700),
        daily_target_min=extract_int(r"Daily Calorie Target[^\d]*([\d,]+)"),
        daily_target_max=extract_int(r"Daily Calorie Target[^\d]*[\d,]+[^\d]*([\d,]+)"),
        deficit_min=extract_int(r"Deficit from Diet[^\d]*([\d,]+)"),
        deficit_max=extract_int(r"Deficit from Diet[^\d]*[\d,]+[^\d]*([\d,]+)"),
        weeks=extract_int(r"(\d+)\s*[Ww]eeks?", 8),
    )
