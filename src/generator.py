"""
HTML generator: renders templates/index.html.j2 → output/index.html
using the loaded recipe data.
"""

import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from .models import Recipe


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
) -> None:
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=False,        # data comes from controlled YAML, not user input
        keep_trailing_newline=True,
    )
    template = env.get_template("index.html.j2")

    recipes_js_dict = {r.id: _recipe_to_js(r) for r in recipes}
    recipes_json = json.dumps(recipes_js_dict, ensure_ascii=False, indent=2)

    html = template.render(recipes=recipes, recipes_json=recipes_json)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
