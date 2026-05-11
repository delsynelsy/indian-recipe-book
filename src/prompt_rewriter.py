"""Gemini-driven prompt rewriter for recipe-card watercolor illustrations.

Takes a Recipe (Spanish name + ingredients + tip) and produces a hand-tuned
positive/negative prompt pair that:
  - locks the style to soft watercolor
  - keeps the dish identity and its actual ingredients intact (no fusion drift)
  - forbids text/labels/logos in the rendered image
  - injects Indian cultural context

Results are cached to data/recipe_image_prompts.json keyed by recipe.id so
re-runs cost nothing.

Usage:
    python -m src.prompt_rewriter --id chilla
    python -m src.prompt_rewriter --all
    python -m src.prompt_rewriter --all --refresh
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types as gtypes

from src.models import Recipe
from src.parser import load_recipes


ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data" / "recipes.yaml"
CACHE_FILE = ROOT / "data" / "recipe_image_prompts.json"

DEFAULT_MODEL = os.environ.get("GEMINI_SMART_MODEL", "gemini-2.5-pro")


PROMPT_SYSTEM = """You are a prompt engineer for a watercolor illustration system that renders
authentic Indian recipe cards. Given a recipe (name, subtitle, ingredients, tip),
you write a `positive` prompt and a `negative` prompt for a Flux2 diffusion model.

RULES (do not violate):

1. STYLE is locked to soft watercolor. Always include language like: "soft
   watercolor illustration, gentle brush strokes, delicate paper texture,
   muted warm palette of cream / sage / terracotta / muted gold, hand-painted,
   hand-drawn line work, soft natural lighting, no harsh shadows". NEVER ask
   for photoreal, CGI, 3D render, anime, manga, cartoon, oil painting, or
   sketch.

2. RECIPE FIDELITY is absolute. The named dish must be the named dish.
   Translate the Spanish dish name to its real Indian-cuisine name if needed
   (e.g. "Tortitas proteicas de lentejas verdes" -> "Moong Dal Chilla, a
   savoury yellow mung-bean pancake"). List 3-5 of the actual visible
   ingredients from the recipe by their real culinary names. Never substitute,
   never fuse cuisines (no sushi, no pizza, no burgers), never invent
   ingredients.

3. COMPOSITION is always: single hero dish, 3/4 overhead view, served on a
   rustic ceramic plate or banana leaf, plain wooden table surface, soft
   diffused light, balanced framing.

4. TEXT AND LABELS are forbidden in the image: no writing on plates, no
   brand names, no logos, no menu captions, no recipe titles painted in.
   ALWAYS include strong negative weights against text.

5. CULTURAL CONTEXT is traditional Indian where natural. Allowed props:
   small copper or brass spoon, fresh coriander sprig, a halved lime, a
   small clay bowl of chutney. Avoid western diner aesthetics.

6. NEGATIVE PROMPT must always carry these weighted artefacts:
   (text:1.8), (writing:1.7), (letters:1.7), (logo:1.5), (brand label:1.5),
   (caption:1.5), (food packaging:1.5), (oversaturated:1.4), (neon:1.5),
   (cgi:1.3), (plastic food:1.4), (cartoon:1.3), (anime:1.4), (manga:1.3),
   (3d render:1.3), (oil painting:1.3), (photo:1.4), (photorealistic:1.4),
   (fusion food:1.5), (sushi:1.6), (pizza:1.6), (burger:1.6),
   (chinese characters:1.6), (japanese characters:1.6),
   blurry, lowres, deformed, mutated, extra limbs, ugly, watermark, signature.

OUTPUT FORMAT: JSON only. No prose, no markdown fences.
Schema:
  positive       string  // 60-120 words, comma-separated descriptors
  negative       string  // 40-80 words, comma-separated weighted artifacts
  scene_summary  string  // one short sentence, for logging
"""


@dataclass
class RewrittenPrompt:
    positive: str
    negative: str
    scene_summary: str


_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["positive", "negative", "scene_summary"],
    "properties": {
        "positive": {"type": "string"},
        "negative": {"type": "string"},
        "scene_summary": {"type": "string"},
    },
}


def _recipe_payload(recipe: Recipe) -> str:
    highlight = ", ".join(recipe.highlight_ingredients) if recipe.highlight_ingredients else ""
    ingredient_lines = "\n  - ".join(recipe.ingredients)
    return (
        f"Recipe:\n"
        f"  name: {recipe.name}\n"
        f"  subtitle: {recipe.subtitle}\n"
        f"  type: {recipe.type}\n"
        f"  meal: {recipe.meal}\n"
        f"  highlight_ingredients: {highlight}\n"
        f"  ingredients:\n  - {ingredient_lines}\n"
        f"  tip: {recipe.tip}\n"
    )


def _load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save_cache(cache: dict) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


_TEMPLATE_RE = __import__("re").compile(r"^\{[^}]+\}$")


def _scrub_template_env() -> None:
    """Drop env vars whose values are literal '{placeholder}' strings - shell
    init can inject them and they'd otherwise shadow real .env values.
    """
    for k, v in list(os.environ.items()):
        if isinstance(v, str) and _TEMPLATE_RE.match(v):
            del os.environ[k]


def _client() -> genai.Client:
    _scrub_template_env()
    load_dotenv(ROOT / ".env", override=True)
    load_dotenv(Path("/home/asjad/projects/inburgeren-prep/api/.env"), override=False)
    key = os.environ.get("GEMINI_API_KEY")
    if not key or _TEMPLATE_RE.match(key):
        raise RuntimeError(
            "GEMINI_API_KEY not set (or still a {placeholder}). Put it in "
            "~/projects/indian-recipe-book/.env or export it. Reuse the key "
            "from inburgeren-prep/api/.env."
        )
    return genai.Client(api_key=key)


def rewrite(
    recipe: Recipe,
    *,
    client: genai.Client | None = None,
    model: str = DEFAULT_MODEL,
    cache: dict | None = None,
    refresh: bool = False,
) -> RewrittenPrompt:
    """Get watercolor prompt pair for one recipe. Cached unless refresh=True."""
    if cache is None:
        cache = _load_cache()
    if not refresh and recipe.id in cache:
        c = cache[recipe.id]
        return RewrittenPrompt(c["positive"], c["negative"], c["scene_summary"])

    client = client or _client()
    response = client.models.generate_content(
        model=model,
        contents=[PROMPT_SYSTEM, _recipe_payload(recipe)],
        config=gtypes.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=_RESPONSE_SCHEMA,
            temperature=0.4,
        ),
    )
    data = json.loads(response.text or "{}")
    result = RewrittenPrompt(
        positive=data["positive"],
        negative=data["negative"],
        scene_summary=data["scene_summary"],
    )
    cache[recipe.id] = asdict(result)
    _save_cache(cache)
    return result


def rewrite_all(recipes: list[Recipe], *, refresh: bool = False) -> dict[str, RewrittenPrompt]:
    client = _client()
    cache = _load_cache()
    out: dict[str, RewrittenPrompt] = {}
    for r in recipes:
        out[r.id] = rewrite(r, client=client, cache=cache, refresh=refresh)
        print(f"  {r.id:20s} {out[r.id].scene_summary[:90]}")
    return out


def main() -> None:
    p = argparse.ArgumentParser(description="Gemini watercolor prompt rewriter")
    p.add_argument("--id", help="Single recipe id (default: all)")
    p.add_argument("--all", action="store_true", help="Rewrite all recipes")
    p.add_argument("--refresh", action="store_true", help="Ignore cache, call Gemini")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Gemini model (default {DEFAULT_MODEL})")
    args = p.parse_args()

    recipes = load_recipes(DATA_FILE)

    if args.id:
        target = next((r for r in recipes if r.id == args.id), None)
        if not target:
            print(f"recipe id '{args.id}' not found. Available: {[r.id for r in recipes]}", file=sys.stderr)
            sys.exit(1)
        result = rewrite(target, model=args.model, refresh=args.refresh)
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return

    if args.all or args.id is None:
        rewrite_all(recipes, refresh=args.refresh)
        print(f"cached -> {CACHE_FILE}")


if __name__ == "__main__":
    main()
