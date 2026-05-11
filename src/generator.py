"""
HTML generator: renders templates/index.html.j2 → output/index.html
using the loaded recipe data.
"""

import json
import re
import unicodedata
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from .models import Recipe


_ROOT = Path(__file__).resolve().parent.parent
_INGREDIENT_MAP_FILE = _ROOT / "data" / "ingredient_image_map.json"
_STEP_MAP_FILE = _ROOT / "data" / "step_image_map.json"


def _slugify(s: str) -> str:
    norm = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    norm = re.sub(r"[^\w\s-]", "", norm.lower())
    return re.sub(r"\s+", "_", norm.strip())[:60]


def _load_ingredient_icons() -> dict[str, str]:
    """slug -> public URL. Empty when map file is missing (icons disabled)."""
    if not _INGREDIENT_MAP_FILE.exists():
        return {}
    raw = json.loads(_INGREDIENT_MAP_FILE.read_text(encoding="utf-8"))
    return {slug: entry["url"] for slug, entry in raw.items()}


def _load_step_icons() -> dict[str, dict]:
    """slug -> {url, action, text_preview}. Empty when map file is missing.

    Slug format: "<recipe_id>__<step_index_zero_padded>".
    """
    if not _STEP_MAP_FILE.exists():
        return {}
    return json.loads(_STEP_MAP_FILE.read_text(encoding="utf-8"))


def _recipe_to_js(recipe: Recipe) -> dict:
    """Convert a Recipe to the JS-compatible dict that preview.html expects."""
    return {
        "name": recipe.name,
        "sub": recipe.subtitle,
        "phase": recipe.phase,
        "meal": recipe.meal,
        "type": recipe.type,
        "prep": recipe.prep,
        "prepNote": recipe.prep_note,
        "cook": recipe.cook,
        "servings": recipe.servings,
        "kcal": recipe.nutrition.kcal,
        "p": recipe.nutrition.protein,
        "c": recipe.nutrition.carbs,
        "g": recipe.nutrition.fat,
        "img": recipe.image.src,
        "imgCls": recipe.image.css_class,
        "ingredients": recipe.ingredients,
        "steps": [
            {"ic": s.icon, "act": s.action, "t": s.time, "txt": s.text}
            for s in recipe.steps
        ],
        "tip": recipe.tip,
    }


def generate_html(
    recipes: list,
    template_dir: Path,
    output_path: Path,
    meal_plan_html: str = "",
    template_name: str = "index.html.j2",
) -> None:
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=False,
        keep_trailing_newline=True,
    )
    env.filters["ing_slug"] = _slugify
    env.filters["zeropad"] = lambda n, w=2: str(int(n)).zfill(w)
    template = env.get_template(template_name)

    icons = _load_ingredient_icons()
    step_icons = _load_step_icons()

    recipes_js_dict = {r.id: _recipe_to_js(r) for r in recipes}
    recipes_json = json.dumps(recipes_js_dict, ensure_ascii=False, indent=2)
    icons_json = json.dumps(icons, ensure_ascii=False, indent=2)
    step_icons_json = json.dumps(step_icons, ensure_ascii=False, indent=2)

    html = template.render(
        recipes=recipes,
        recipes_json=recipes_json,
        ingredient_icons=icons,
        ingredient_icons_json=icons_json,
        step_icons=step_icons,
        step_icons_json=step_icons_json,
        meal_plan_html=meal_plan_html,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
