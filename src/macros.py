"""
Macro calculation and validation utilities.

Atwater factors used:
  Protein:      4 kcal/g
  Carbohydrates: 4 kcal/g
  Fat:           9 kcal/g
"""

from .models import Recipe, MealPlanProfile


TOLERANCE_PCT = 0.15  # 15% tolerance for macro/kcal validation


def calories_from_macros(protein: int, carbs: int, fat: int) -> int:
    return protein * 4 + carbs * 4 + fat * 9


def validate_recipe(recipe: Recipe) -> list:
    """Return list of warning strings; empty means all good."""
    warnings = []
    calculated = calories_from_macros(
        recipe.nutrition.protein,
        recipe.nutrition.carbs,
        recipe.nutrition.fat,
    )
    actual = recipe.nutrition.kcal
    diff_pct = abs(calculated - actual) / max(actual, 1) * 100
    if diff_pct > TOLERANCE_PCT * 100:
        warnings.append(
            f"[{recipe.name}] kcal mismatch: "
            f"P{recipe.nutrition.protein}×4 + C{recipe.nutrition.carbs}×4 + "
            f"F{recipe.nutrition.fat}×9 = {calculated} kcal "
            f"(stated {actual}, diff {diff_pct:.0f}%)"
        )
    if not recipe.steps:
        warnings.append(f"[{recipe.name}] No steps defined.")
    if not recipe.ingredients:
        warnings.append(f"[{recipe.name}] No ingredients defined.")
    return warnings


def validate_all(recipes: list) -> list:
    issues = []
    for r in recipes:
        issues.extend(validate_recipe(r))
    return issues


def nutrition_summary(recipes: list) -> dict:
    n = len(recipes)
    if n == 0:
        return {}
    return {
        "count": n,
        "total_kcal": sum(r.nutrition.kcal for r in recipes),
        "avg_kcal": sum(r.nutrition.kcal for r in recipes) / n,
        "avg_protein_g": sum(r.nutrition.protein for r in recipes) / n,
        "avg_carbs_g": sum(r.nutrition.carbs for r in recipes) / n,
        "avg_fat_g": sum(r.nutrition.fat for r in recipes) / n,
    }


def bmr_mifflin(weight_kg: float, height_cm: float, age: int, female: bool = True) -> float:
    """Mifflin-St Jeor BMR formula."""
    base = 10 * weight_kg + 6.25 * height_cm - 5 * age
    return base - 161 if female else base + 5


def tdee(bmr: float, activity: str = "light") -> float:
    """
    Activity multipliers:
      sedentary: 1.2 | light: 1.375 | moderate: 1.55 | active: 1.725
    """
    factors = {"sedentary": 1.2, "light": 1.375, "moderate": 1.55, "active": 1.725}
    return bmr * factors.get(activity, 1.375)


def daily_deficit_needed(weight_to_lose_kg: float, weeks: int) -> float:
    """1 kg fat ≈ 7,700 kcal. Returns daily deficit required."""
    total_kcal = weight_to_lose_kg * 7700
    days = weeks * 7
    return total_kcal / days


def plan_feasibility(profile: MealPlanProfile) -> dict:
    """Assess whether the plan target is safe and realistic."""
    calculated_bmr = bmr_mifflin(
        profile.current_weight, profile.height, profile.age
    )
    calculated_tdee = tdee(calculated_bmr, "light")
    required_deficit = daily_deficit_needed(profile.weight_to_lose, profile.weeks)
    min_safe_intake = 1200  # kcal/day minimum for women
    projected_intake = calculated_tdee - required_deficit

    return {
        "calculated_bmr": round(calculated_bmr),
        "calculated_tdee": round(calculated_tdee),
        "required_daily_deficit": round(required_deficit),
        "projected_daily_intake": round(projected_intake),
        "is_safe": projected_intake >= min_safe_intake,
        "note": (
            "Plan is within safe range."
            if projected_intake >= min_safe_intake
            else f"Warning: projected intake {projected_intake:.0f} kcal < {min_safe_intake} kcal minimum."
        ),
    }
