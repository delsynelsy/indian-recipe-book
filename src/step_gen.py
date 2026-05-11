"""Per-step watercolor vignette generator for Procedimiento.

Each of the 48 cooking steps gets its own watercolor illustration reflecting
the specific action and ingredients from `step.text`. Replaces the generic
inline SVG icons currently rendered by `buildIcon(s.ic)`.

Pipeline mirrors src/ingredient_gen.py:

  1. Gemini 2.5 Pro reads {recipe.name, action, text, highlight_ingredients}
     and emits a JSON {positive, negative, scene_summary} watercolor prompt
     for a single-action vignette (hands/utensils performing the action with
     the actual food in-frame).
  2. Flux2 Klein 9B fp8 renders 256x256 via ComfyUI HTTP API.
  3. Output webp lands at /mnt/nas/recipe-book/assets/steps/<slug>.webp where
     the existing nginx + Cloudflare tunnel serves it publicly at
     https://images.mohammadasjad.com/steps/<slug>.webp.

Slug format: f"{recipe_id}__{idx:02d}" (e.g. chilla__00, rajma__04).

Usage:
    python -m src.step_gen list                    # show all 48 rows
    python -m src.step_gen rewrite                 # Gemini prompts (cached)
    python -m src.step_gen gen --slug chilla__04   # one image
    python -m src.step_gen gen                     # all missing
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

from google.genai import types as gtypes

from src.parser import load_recipes
from src.prompt_rewriter import (
    _client as _gemini_client,
    _scrub_template_env,
)


ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data" / "recipes.yaml"

NAS_STEPS = Path("/mnt/nas/recipe-book/assets/steps")
LOCAL_MIRROR = ROOT / "images" / "steps"
PROMPT_CACHE = ROOT / "data" / "step_prompts.json"
URL_MAP = ROOT / "data" / "step_image_map.json"
PUBLIC_BASE = "https://images.mohammadasjad.com/steps"

COMFY_URL = os.environ.get("COMFY_URL", "http://127.0.0.1:8188")

# 256x256 - renders into a 48px icon slot at 5x density; 256 is divisible by 64
# so Flux2's EmptyFlux2LatentImage accepts it. Smaller than ingredient icons
# because 48 images in a batch must run quickly.
STEP_WIDTH = 256
STEP_HEIGHT = 256
STEP_FLUX_STEPS = 6
STEP_CFG = 1.0
STEP_SAMPLER = "euler"
STEP_SCHEDULER = "simple"

FLUX_UNET = "flux-2-klein-9b-fp8.safetensors"
FLUX_CLIP = "qwen_3_8b_fp8mixed.safetensors"
FLUX_CLIP_TYPE = "flux2"
FLUX_VAE = "flux2-vae.safetensors"

WEBP_QUALITY = 88


# ── data flatten ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class StepRow:
    recipe_id: str
    recipe_name: str
    idx: int
    icon: str
    action: str
    time: str
    text: str
    highlight_ingredients: tuple[str, ...]

    @property
    def slug(self) -> str:
        return f"{self.recipe_id}__{self.idx:02d}"


def unique_steps() -> list[StepRow]:
    rows: list[StepRow] = []
    for r in load_recipes(DATA_FILE):
        for i, s in enumerate(r.steps):
            rows.append(StepRow(
                recipe_id=r.id,
                recipe_name=r.name,
                idx=i,
                icon=s.icon,
                action=s.action,
                time=s.time,
                text=s.text,
                highlight_ingredients=tuple(r.highlight_ingredients),
            ))
    return rows


# ── Gemini prompt rewriter ──────────────────────────────────────────────────

STEP_PROMPT_SYSTEM = """You are a prompt engineer for a soft watercolor illustration system that
renders one icon per cooking step in an Indian recipe book.

Given a step (recipe name, action verb, Spanish text, available ingredients)
produce a positive + negative Flux2 prompt for a 256x256 watercolor vignette.

RULES (do not violate):

1. SINGLE ACTION moment. Frame the icon TIGHT: hands or a utensil performing
   the action, with the actual food in-frame. No wide kitchen scenes, no
   people from the waist up, no multiple actions. Examples:
   - "Verter la masa" -> close-up of two hands pouring yellow mung-bean
     batter from a ladle onto a hot iron pan.
   - "Remojar el dal" -> shallow ceramic bowl half-full of dried yellow
     mung beans soaking in clear water, top-down view.
   - "Sofreír la cebolla" -> wooden spoon stirring sliced red onions
     turning golden in a small iron kadai.

2. RECIPE FIDELITY. Use the ACTUAL ingredients from the recipe's highlight
   list (translate Spanish -> Indian cuisine names when natural). NEVER
   substitute. NEVER fuse cuisines. NEVER invent unlisted ingredients.

3. STYLE is locked to soft watercolor: gentle brush strokes, paper texture,
   muted warm palette of cream / sage / terracotta / muted gold, hand-painted,
   hand-drawn line work, soft natural lighting, no harsh shadows. NEVER ask
   for photoreal, CGI, 3D render, anime, cartoon, oil painting.

4. COMPOSITION: square 1:1 framing. Single hero subject centered. Cream
   parchment background or simple wood surface. Small soft shadow under
   the subject OK.

5. TEXT and LABELS forbidden. NO writing on bowls, pans, packaging, or
   anywhere in the image. NO logos. NO brand names. NO recipe captions.

6. HANDS rendered with correct anatomy when present (5 fingers, natural
   pose, no extra limbs). If anatomy is risky for a step, prefer a
   utensil-only composition instead of hands.

7. NEGATIVE PROMPT must carry these weighted artefacts:
   (text:1.8), (writing:1.7), (letters:1.7), (logo:1.5), (caption:1.5),
   (food packaging:1.5), (label:1.5), (oversaturated:1.4), (neon:1.5),
   (cgi:1.3), (plastic:1.4), (cartoon:1.3), (anime:1.4), (3d render:1.3),
   (photo:1.4), (photorealistic:1.4), (chinese characters:1.6),
   (japanese characters:1.6), (deformed hands:1.7), (extra fingers:1.7),
   (mutated:1.4), (multiple actions:1.5), (cluttered:1.4),
   (wide kitchen scene:1.5), (people faces:1.4),
   blurry, lowres, deformed, watermark, signature, fork, knife.

OUTPUT FORMAT: JSON only. Schema:
  positive       string  // 50-110 words, comma-separated descriptors
  negative       string  // 40-80 words, comma-separated weighted artifacts
  scene_summary  string  // one short English sentence describing the frame
"""


_STEP_SCHEMA = {
    "type": "object",
    "required": ["positive", "negative", "scene_summary"],
    "properties": {
        "positive": {"type": "string"},
        "negative": {"type": "string"},
        "scene_summary": {"type": "string"},
    },
}


@dataclass
class StepPrompt:
    positive: str
    negative: str
    scene_summary: str


def _row_payload(row: StepRow) -> str:
    ings = ", ".join(row.highlight_ingredients) if row.highlight_ingredients else ""
    return (
        f"Recipe: {row.recipe_name} (id={row.recipe_id})\n"
        f"Step {row.idx + 1}:\n"
        f"  action: {row.action}\n"
        f"  icon hint: {row.icon}\n"
        f"  time: {row.time or '-'}\n"
        f"  text (Spanish): {row.text}\n"
        f"  available ingredients in this recipe: {ings}\n"
    )


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


def rewrite_one(row: StepRow, client, model: str, cache: dict, *, refresh: bool = False) -> StepPrompt:
    slug = row.slug
    if not refresh and slug in cache:
        c = cache[slug]
        return StepPrompt(c["positive"], c["negative"], c["scene_summary"])
    response = client.models.generate_content(
        model=model,
        contents=[STEP_PROMPT_SYSTEM, _row_payload(row)],
        config=gtypes.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=_STEP_SCHEMA,
            temperature=0.4,
        ),
    )
    data = json.loads(response.text or "{}")
    result = StepPrompt(
        positive=data["positive"],
        negative=data["negative"],
        scene_summary=data["scene_summary"],
    )
    cache[slug] = asdict(result)
    _save_prompt_cache(cache)
    return result


def rewrite_all(refresh: bool = False) -> dict[str, StepPrompt]:
    client = _gemini_client()
    model = os.environ.get("GEMINI_SMART_MODEL", "gemini-2.5-pro")
    cache = _load_prompt_cache()
    rows = unique_steps()
    out: dict[str, StepPrompt] = {}
    for row in rows:
        out[row.slug] = rewrite_one(row, client, model, cache, refresh=refresh)
        print(f"  {row.slug:18s} :: {out[row.slug].scene_summary[:90]}")
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
            "inputs": {"width": STEP_WIDTH, "height": STEP_HEIGHT, "batch_size": 1},
        },
        "7": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["1", 0], "positive": ["4", 0], "negative": ["5", 0],
                "latent_image": ["6", 0], "seed": seed, "steps": STEP_FLUX_STEPS,
                "cfg": STEP_CFG, "sampler_name": STEP_SAMPLER, "scheduler": STEP_SCHEDULER,
                "denoise": 1.0,
            },
        },
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": "step"}},
    }


def _seed_for_slug(slug: str) -> int:
    h = hashlib.sha256(slug.encode("utf-8")).digest()
    return int.from_bytes(h[:4], "big")


async def _submit(client: httpx.AsyncClient, workflow: dict[str, Any]) -> str:
    r = await client.post(f"{COMFY_URL}/prompt", json={"prompt": workflow, "client_id": str(uuid.uuid4())})
    r.raise_for_status()
    return r.json()["prompt_id"]


async def _poll(client: httpx.AsyncClient, pid: str, timeout_s: int = 600) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = await client.get(f"{COMFY_URL}/history/{pid}")
        if r.status_code == 200:
            d = r.json()
            if pid in d:
                return d[pid]
        await asyncio.sleep(1.5)
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
    async with httpx.AsyncClient(timeout=600.0) as client:
        pid = await _submit(client, wf)
        entry = await _poll(client, pid)
        return await _download(client, entry, png_path)


def _to_webp(png_path: Path, webp_path: Path) -> None:
    img = Image.open(png_path).convert("RGB")
    img.save(webp_path, "WEBP", quality=WEBP_QUALITY, method=6)
    png_path.unlink(missing_ok=True)


def generate_one(row: StepRow, prompt: StepPrompt) -> Path:
    NAS_STEPS.mkdir(parents=True, exist_ok=True)
    LOCAL_MIRROR.mkdir(parents=True, exist_ok=True)
    png_path = LOCAL_MIRROR / f"{row.slug}.png"
    webp_path = NAS_STEPS / f"{row.slug}.webp"
    local_webp = LOCAL_MIRROR / f"{row.slug}.webp"
    seed = _seed_for_slug(row.slug)
    print(f"  [flux2] {row.slug:18s} seed={seed} :: {prompt.scene_summary[:80]}")
    asyncio.run(_gen_async(prompt.positive, prompt.negative, seed, png_path))
    _to_webp(png_path, webp_path)
    shutil.copyfile(webp_path, local_webp)
    return webp_path


def generate_all(override: bool = False) -> None:
    client = _gemini_client()
    model = os.environ.get("GEMINI_SMART_MODEL", "gemini-2.5-pro")
    cache = _load_prompt_cache()
    rows = unique_steps()
    NAS_STEPS.mkdir(parents=True, exist_ok=True)

    url_map: dict[str, dict] = {}
    for row in rows:
        webp = NAS_STEPS / f"{row.slug}.webp"
        if not override and webp.exists() and webp.stat().st_size > 4096:
            print(f"  skip {row.slug}")
        else:
            prompt = rewrite_one(row, client, model, cache)
            try:
                generate_one(row, prompt)
            except Exception as e:
                print(f"  FAIL {row.slug}: {str(e)[:160]}", file=sys.stderr)
                continue
        url_map[row.slug] = {
            "url": f"{PUBLIC_BASE}/{row.slug}.webp",
            "action": row.action,
            "text_preview": row.text[:60],
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
    p = argparse.ArgumentParser(description="Per-step watercolor icons")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="show all 48 (recipe, step) rows + slugs")
    r = sub.add_parser("rewrite", help="Gemini prompt rewrite all (cached)")
    r.add_argument("--refresh", action="store_true")
    g = sub.add_parser("gen", help="Generate images (Gemini + Flux2)")
    g.add_argument("--slug", help="just one slug (e.g. chilla__04)")
    g.add_argument("--override", action="store_true")

    args = p.parse_args()

    if args.cmd == "list":
        for row in unique_steps():
            print(f"  {row.slug:18s} {row.icon:10s} {row.action:18s} :: {row.text[:80]}")
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
            rows = {r.slug: r for r in unique_steps()}
            if args.slug not in rows:
                print(f"slug '{args.slug}' not found. Available: {sorted(rows)[:5]}...", file=sys.stderr)
                sys.exit(1)
            row = rows[args.slug]
            cache = _load_prompt_cache()
            client = _gemini_client()
            model = os.environ.get("GEMINI_SMART_MODEL", "gemini-2.5-pro")
            prompt = rewrite_one(row, client, model, cache)
            path = generate_one(row, prompt)
            print(f"saved {path}")
        else:
            generate_all(override=args.override)


if __name__ == "__main__":
    main()
