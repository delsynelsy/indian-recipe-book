"""Per-ingredient watercolor icon generator.

Dedupes Recipe.highlight_ingredients across all recipes, asks Gemini to write
a single-object watercolor prompt (no plate, no scene, just the ingredient on
cream parchment), renders via Flux2 ComfyUI workflow at 512x512, transcodes to
webp@88. Output goes to /mnt/nas/recipe-book/assets/ingredients/<slug>.webp so
the dockerized nginx on NAS can serve them at https://images.mohammadasjad.com/
ingredients/<slug>.webp.

Mapping (slug -> public URL) is written to data/ingredient_image_map.json so
the Jinja template can render <img> per ingredient bullet.

Usage:
    python -m src.ingredient_gen list                # show dedup + slugs
    python -m src.ingredient_gen rewrite             # Gemini prompt rewrite all
    python -m src.ingredient_gen gen --slug cilantro # one image
    python -m src.ingredient_gen gen                 # all missing
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

from src.parser import load_recipes
from src.prompt_rewriter import (
    _client as _gemini_client,
    _scrub_template_env,
)
from google.genai import types as gtypes


ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data" / "recipes.yaml"

NAS_ASSETS = Path("/mnt/nas/recipe-book/assets/ingredients")
LOCAL_MIRROR = ROOT / "images" / "ingredients"
PROMPT_CACHE = ROOT / "data" / "ingredient_prompts.json"
URL_MAP = ROOT / "data" / "ingredient_image_map.json"
PUBLIC_BASE = "https://images.mohammadasjad.com/ingredients"

COMFY_URL = os.environ.get("COMFY_URL", "http://127.0.0.1:8188")

# Smaller, square, fast - these are icons (32-48px in UI, source 2x density 512).
ING_WIDTH = 512
ING_HEIGHT = 512
ING_STEPS = 6
ING_CFG = 1.0
ING_SAMPLER = "euler"
ING_SCHEDULER = "simple"

FLUX_UNET = "flux-2-klein-9b-fp8.safetensors"
FLUX_CLIP = "qwen_3_8b_fp8mixed.safetensors"
FLUX_CLIP_TYPE = "flux2"
FLUX_VAE = "flux2-vae.safetensors"

WEBP_QUALITY = 88


# ── slug + dedup ────────────────────────────────────────────────────────────

def _slugify(s: str) -> str:
    """ASCII-fold + lowercase + underscored. Stable across runs."""
    norm = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    norm = re.sub(r"[^\w\s-]", "", norm.lower())
    norm = re.sub(r"\s+", "_", norm.strip())
    return norm[:60] or "unknown"


def unique_ingredients() -> dict[str, dict[str, Any]]:
    """Map slug -> {'label': str, 'recipes': list[str]}. Dedupe across recipes."""
    recipes = load_recipes(DATA_FILE)
    out: dict[str, dict[str, Any]] = {}
    for r in recipes:
        for raw in r.highlight_ingredients:
            label = raw.strip()
            slug = _slugify(label)
            entry = out.setdefault(slug, {"label": label, "recipes": []})
            if r.id not in entry["recipes"]:
                entry["recipes"].append(r.id)
    return out


# ── Gemini single-object prompt rewriter ────────────────────────────────────

ING_PROMPT_SYSTEM = """You are a prompt engineer for a soft watercolor illustration system that
renders single-ingredient icons for an Indian recipe book.

Given one ingredient (Spanish label, possibly idiomatic), produce a Flux2
positive + negative prompt for a 512x512 watercolor stamp.

RULES (do not violate):

1. Translate the Spanish label to its real culinary ingredient (e.g. "Dal
   moong" -> "yellow mung bean lentils", "Jengibre" -> "fresh ginger root",
   "Cilantro" -> "fresh coriander leaves", "Pechuga pollo" -> "raw boneless
   chicken breast", "Ghee" -> "small clay bowl of clarified butter (ghee)",
   "Garam masala" -> "small mound of garam masala spice powder").

2. STYLE: soft watercolor illustration, gentle brush strokes, paper texture,
   muted warm palette of cream / sage / terracotta / muted gold, hand-painted,
   hand-drawn line work, soft natural light. ALWAYS include this language.

3. COMPOSITION: single isolated subject, centered on a plain cream parchment
   background. No plate, no bowl unless the ingredient demands it (oils,
   ground spices, yogurt). No table, no props, no people, no scene. Small
   shadow under the subject is OK.

4. FIDELITY: do not substitute. Do not invent. If the label is ambiguous,
   pick the most common Indian-cuisine interpretation.

5. TEXT and LABELS forbidden in the image. NO writing on anything.

6. NEGATIVE PROMPT must carry these weighted artefacts:
   (text:1.8), (writing:1.7), (letters:1.7), (logo:1.5), (caption:1.5),
   (food packaging:1.5), (label:1.5), (price tag:1.5), (oversaturated:1.4),
   (neon:1.5), (cgi:1.3), (plastic:1.4), (cartoon:1.3), (anime:1.4),
   (3d render:1.3), (photo:1.4), (photorealistic:1.4), (chinese characters:1.6),
   (multiple ingredients:1.5), (cluttered:1.4), (background scene:1.4),
   (plate:1.3), (table:1.3), (people:1.5), (hand:1.4),
   blurry, lowres, deformed, watermark.

OUTPUT FORMAT: JSON only. Schema:
  positive       string  // 40-90 words, comma-separated descriptors
  negative       string  // 30-60 words, comma-separated weighted artifacts
  english_name   string  // short canonical English name for logging
"""


_ING_SCHEMA = {
    "type": "object",
    "required": ["positive", "negative", "english_name"],
    "properties": {
        "positive": {"type": "string"},
        "negative": {"type": "string"},
        "english_name": {"type": "string"},
    },
}


@dataclass
class IngredientPrompt:
    positive: str
    negative: str
    english_name: str


def _load_prompt_cache() -> dict:
    if PROMPT_CACHE.exists():
        try:
            return json.loads(PROMPT_CACHE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save_prompt_cache(cache: dict) -> None:
    PROMPT_CACHE.parent.mkdir(parents=True, exist_ok=True)
    PROMPT_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def rewrite_one(label: str, client, model: str, cache: dict, *, refresh: bool = False) -> IngredientPrompt:
    slug = _slugify(label)
    if not refresh and slug in cache:
        c = cache[slug]
        return IngredientPrompt(c["positive"], c["negative"], c["english_name"])
    response = client.models.generate_content(
        model=model,
        contents=[ING_PROMPT_SYSTEM, f"Ingredient label (Spanish): {label}"],
        config=gtypes.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=_ING_SCHEMA,
            temperature=0.4,
        ),
    )
    data = json.loads(response.text or "{}")
    result = IngredientPrompt(
        positive=data["positive"],
        negative=data["negative"],
        english_name=data["english_name"],
    )
    cache[slug] = asdict(result)
    _save_prompt_cache(cache)
    return result


def rewrite_all(refresh: bool = False) -> dict[str, IngredientPrompt]:
    client = _gemini_client()
    model = os.environ.get("GEMINI_SMART_MODEL", "gemini-2.5-pro")
    cache = _load_prompt_cache()
    uniq = unique_ingredients()
    out: dict[str, IngredientPrompt] = {}
    for slug, meta in sorted(uniq.items()):
        out[slug] = rewrite_one(meta["label"], client, model, cache, refresh=refresh)
        print(f"  {slug:25s} :: {out[slug].english_name}")
    return out


# ── ComfyUI Flux2 workflow ──────────────────────────────────────────────────

def _flux_workflow(positive: str, negative: str, seed: int) -> dict[str, Any]:
    full_pos = positive
    if negative:
        full_pos = f"{positive}\n\nSuppress strongly: {negative}"
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": FLUX_UNET, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": FLUX_CLIP, "type": FLUX_CLIP_TYPE}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": FLUX_VAE}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": full_pos}},
        "5": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["4", 0]}},
        "6": {
            "class_type": "EmptyFlux2LatentImage",
            "inputs": {"width": ING_WIDTH, "height": ING_HEIGHT, "batch_size": 1},
        },
        "7": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["1", 0], "positive": ["4", 0], "negative": ["5", 0],
                "latent_image": ["6", 0], "seed": seed, "steps": ING_STEPS, "cfg": ING_CFG,
                "sampler_name": ING_SAMPLER, "scheduler": ING_SCHEDULER, "denoise": 1.0,
            },
        },
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": "ingredient"}},
    }


def _seed_for_slug(slug: str) -> int:
    h = hashlib.sha256(slug.encode("utf-8")).digest()
    return int.from_bytes(h[:4], "big")


async def _submit(client: httpx.AsyncClient, workflow: dict[str, Any]) -> str:
    r = await client.post(f"{COMFY_URL}/prompt", json={"prompt": workflow, "client_id": str(uuid.uuid4())})
    r.raise_for_status()
    return r.json()["prompt_id"]


async def _poll(client: httpx.AsyncClient, pid: str, timeout_s: int = 1800) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = await client.get(f"{COMFY_URL}/history/{pid}")
        if r.status_code == 200:
            d = r.json()
            if pid in d:
                return d[pid]
        await asyncio.sleep(2.0)
    raise TimeoutError(pid)


async def _download(client: httpx.AsyncClient, entry: dict[str, Any], dest: Path) -> Path:
    for _, n in entry.get("outputs", {}).items():
        for img in n.get("images", []) or []:
            p = {
                "filename": img["filename"],
                "subfolder": img.get("subfolder", ""),
                "type": img.get("type", "output"),
            }
            r = await client.get(f"{COMFY_URL}/view", params=p)
            r.raise_for_status()
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(r.content)
            return dest
    raise RuntimeError("no image in outputs")


async def _gen_async(positive: str, negative: str, seed: int, png_path: Path) -> Path:
    wf = _flux_workflow(positive, negative, seed)
    async with httpx.AsyncClient(timeout=1800.0) as client:
        pid = await _submit(client, wf)
        entry = await _poll(client, pid)
        return await _download(client, entry, png_path)


def _to_webp(png_path: Path, webp_path: Path) -> None:
    img = Image.open(png_path).convert("RGB")
    img.save(webp_path, "WEBP", quality=WEBP_QUALITY, method=6)
    png_path.unlink(missing_ok=True)


def generate_one(slug: str, prompt: IngredientPrompt) -> Path:
    NAS_ASSETS.mkdir(parents=True, exist_ok=True)
    LOCAL_MIRROR.mkdir(parents=True, exist_ok=True)
    png_path = LOCAL_MIRROR / f"{slug}.png"
    webp_path = NAS_ASSETS / f"{slug}.webp"
    local_webp = LOCAL_MIRROR / f"{slug}.webp"
    seed = _seed_for_slug(slug)
    print(f"  [flux2] {slug:25s} seed={seed} :: {prompt.english_name}")
    asyncio.run(_gen_async(prompt.positive, prompt.negative, seed, png_path))
    _to_webp(png_path, webp_path)
    # Local mirror for sanity check / fallback when NAS is unreachable.
    import shutil
    shutil.copyfile(webp_path, local_webp)
    return webp_path


def generate_all(override: bool = False) -> None:
    client = _gemini_client()
    model = os.environ.get("GEMINI_SMART_MODEL", "gemini-2.5-pro")
    cache = _load_prompt_cache()
    uniq = unique_ingredients()
    NAS_ASSETS.mkdir(parents=True, exist_ok=True)

    url_map: dict[str, dict] = {}
    for slug, meta in sorted(uniq.items()):
        webp = NAS_ASSETS / f"{slug}.webp"
        if not override and webp.exists() and webp.stat().st_size > 4096:
            print(f"  skip {slug}")
        else:
            prompt = rewrite_one(meta["label"], client, model, cache)
            try:
                generate_one(slug, prompt)
            except Exception as e:
                print(f"  FAIL {slug}: {str(e)[:160]}", file=sys.stderr)
                continue
        url_map[slug] = {
            "label": meta["label"],
            "url": f"{PUBLIC_BASE}/{slug}.webp",
            "recipes": meta["recipes"],
        }
    URL_MAP.write_text(json.dumps(url_map, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"url map -> {URL_MAP}")


def _comfy_health() -> bool:
    try:
        r = httpx.get(f"{COMFY_URL}/system_stats", timeout=3.0)
        return r.status_code == 200
    except Exception:
        return False


def main() -> None:
    p = argparse.ArgumentParser(description="Per-ingredient watercolor icons")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="show dedup slugs + recipe usage")
    sub.add_parser("rewrite", help="Gemini prompt rewrite all (cached)").add_argument(
        "--refresh", action="store_true"
    )
    g = sub.add_parser("gen", help="Generate images (Gemini + Flux2)")
    g.add_argument("--slug", help="just one slug")
    g.add_argument("--override", action="store_true")

    args = p.parse_args()

    if args.cmd == "list":
        for slug, meta in sorted(unique_ingredients().items()):
            print(f"  {slug:30s} {meta['label']:30s} {meta['recipes']}")
        return

    if args.cmd == "rewrite":
        _scrub_template_env()
        rewrite_all(refresh=getattr(args, "refresh", False))
        return

    if args.cmd == "gen":
        if not _comfy_health():
            print(f"ComfyUI not reachable at {COMFY_URL}.", file=sys.stderr)
            sys.exit(2)
        _scrub_template_env()
        if args.slug:
            cache = _load_prompt_cache()
            uniq = unique_ingredients()
            if args.slug not in uniq:
                print(f"slug '{args.slug}' not in {list(uniq.keys())}", file=sys.stderr)
                sys.exit(1)
            client = _gemini_client()
            model = os.environ.get("GEMINI_SMART_MODEL", "gemini-2.5-pro")
            prompt = rewrite_one(uniq[args.slug]["label"], client, model, cache)
            path = generate_one(args.slug, prompt)
            print(f"saved {path}")
        else:
            generate_all(override=args.override)


if __name__ == "__main__":
    main()
