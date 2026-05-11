"""ComfyUI image-gen client for Indian recipe cards.

Backends:
  flux2   - Flux2 Klein 9B fp8 (recommended). Slow on RTX 3070 8GB but produces
            clean watercolor compositions. Run ComfyUI with --lowvram.
  z-image - Z-Image Turbo fp8 fallback (~8 steps, fast). For dev iteration or
            when Flux2 OOMs.

Both backends consume the watercolor prompt pair produced by prompt_rewriter.
Output webp lives at images/<recipe_id>.webp.

Usage:
    python -m src.image_gen one --id chilla
    python -m src.image_gen all
    IMG_BACKEND=z-image python -m src.image_gen one --id chilla
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

from src.models import Recipe
from src.parser import load_recipes
from src.prompt_rewriter import RewrittenPrompt, rewrite, _load_cache, _client as _gemini_client


ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data" / "recipes.yaml"

# Single asset model: heroes go to NAS, served at images.mohammadasjad.com/hero/
NAS_HERO = Path("/mnt/nas/recipe-book/assets/hero")
LOCAL_MIRROR_HERO = ROOT / "images"  # offline fallback when NAS is unreachable
PUBLIC_BASE_HERO = "https://images.mohammadasjad.com/hero"
HERO_URL_MAP = ROOT / "data" / "hero_image_map.json"

# Back-compat alias: callers using OUT_DIR get the NAS path so any future
# direct writes still land on the CDN.
OUT_DIR = NAS_HERO

COMFY_URL = os.environ.get("COMFY_URL", "http://127.0.0.1:8188")
BACKEND = os.environ.get("IMG_BACKEND", "flux2")


# Card image renders at ~360x242 (3:2) in templates/index.html.j2 (.img-wrap
# height:242px, .card-img object-fit:cover). Generate at 3:2 to minimise crop
# waste and target ~2x display density so it looks sharp on HiDPI screens.
# 1152x768 = 3:2, divisible by 64 (Flux2 latent multiple), ~884k pixels.
@dataclass(frozen=True)
class FluxFoodPreset:
    unet: str = "flux-2-klein-9b-fp8.safetensors"
    weight_dtype: str = "default"
    clip_name: str = "qwen_3_8b_fp8mixed.safetensors"
    clip_type: str = "flux2"
    vae: str = "flux2-vae.safetensors"
    width: int = 1152
    height: int = 768
    steps: int = 6
    cfg: float = 1.0
    sampler: str = "euler"
    scheduler: str = "simple"


# Z-Image turbo distill: 8 steps cfg 1.2 is the published recipe. 1024x680
# (~3:2) keeps it under 8 GB VRAM on the 3070 while matching the card aspect.
@dataclass(frozen=True)
class ZImageFoodPreset:
    checkpoint: str = "z-image/z-image-turbo_fp8_scaled_e4m3fn_KJ.safetensors"
    width: int = 1024
    height: int = 680
    steps: int = 8
    cfg: float = 1.2
    sampler: str = "euler"
    scheduler: str = "simple"


# Webp quality 88: tuned for watercolor texture preservation. Lossy webp
# below ~85 smudges the paper-grain detail Gemini's prompts emphasise.
WEBP_QUALITY = 88


FLUX = FluxFoodPreset()
ZIMG = ZImageFoodPreset()


def _seed_for_recipe(recipe_id: str) -> int:
    h = hashlib.sha256(recipe_id.encode("utf-8")).digest()
    return int.from_bytes(h[:4], "big")


def _build_flux_workflow(positive: str, negative: str, seed: int) -> dict[str, Any]:
    """Flux2 Klein 9B txt2img graph. Negative is zeroed-out conditioning per
    distilled-flux best practice, then a second pass adds the real negative -
    but Flux2 fp8 distill prefers ConditioningZeroOut, so we mirror inburgeren
    and pass negative as zeroed. The hard negative tokens are added to the
    positive prompt's suffix as suppression hints instead.
    """
    # Inject hard negative suppression directly into positive's tail because
    # Flux2 distilled at cfg=1.0 does not honour a separate negative branch.
    full_pos = positive
    if negative:
        full_pos = f"{positive}\n\nSuppress strongly: {negative}"

    return {
        "1": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": FLUX.unet, "weight_dtype": FLUX.weight_dtype},
        },
        "2": {
            "class_type": "CLIPLoader",
            "inputs": {"clip_name": FLUX.clip_name, "type": FLUX.clip_type},
        },
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": FLUX.vae}},
        "4": {
            "class_type": "CLIPTextEncode",
            "inputs": {"clip": ["2", 0], "text": full_pos},
        },
        "5": {
            "class_type": "ConditioningZeroOut",
            "inputs": {"conditioning": ["4", 0]},
        },
        "6": {
            "class_type": "EmptyFlux2LatentImage",
            "inputs": {"width": FLUX.width, "height": FLUX.height, "batch_size": 1},
        },
        "7": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["1", 0],
                "positive": ["4", 0],
                "negative": ["5", 0],
                "latent_image": ["6", 0],
                "seed": seed,
                "steps": FLUX.steps,
                "cfg": FLUX.cfg,
                "sampler_name": FLUX.sampler,
                "scheduler": FLUX.scheduler,
                "denoise": 1.0,
            },
        },
        "8": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["7", 0], "vae": ["3", 0]},
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {"images": ["8", 0], "filename_prefix": "recipe"},
        },
    }


def _build_zimage_workflow(positive: str, negative: str, seed: int) -> dict[str, Any]:
    return {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": ZIMG.checkpoint},
        },
        "2": {
            "class_type": "CLIPTextEncode",
            "inputs": {"clip": ["1", 1], "text": positive},
        },
        "3": {
            "class_type": "CLIPTextEncode",
            "inputs": {"clip": ["1", 1], "text": negative},
        },
        "4": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": ZIMG.width, "height": ZIMG.height, "batch_size": 1},
        },
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["1", 0],
                "positive": ["2", 0],
                "negative": ["3", 0],
                "latent_image": ["4", 0],
                "seed": seed,
                "steps": ZIMG.steps,
                "cfg": ZIMG.cfg,
                "sampler_name": ZIMG.sampler,
                "scheduler": ZIMG.scheduler,
                "denoise": 1.0,
            },
        },
        "6": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["5", 0], "vae": ["1", 2]},
        },
        "7": {
            "class_type": "SaveImage",
            "inputs": {"images": ["6", 0], "filename_prefix": "recipe_zimg"},
        },
    }


async def _submit(client: httpx.AsyncClient, workflow: dict[str, Any]) -> str:
    payload = {"prompt": workflow, "client_id": str(uuid.uuid4())}
    r = await client.post(f"{COMFY_URL}/prompt", json=payload)
    r.raise_for_status()
    return r.json()["prompt_id"]


async def _poll(client: httpx.AsyncClient, prompt_id: str, timeout_s: int = 1800) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = await client.get(f"{COMFY_URL}/history/{prompt_id}")
        if r.status_code == 200:
            data = r.json()
            if prompt_id in data:
                return data[prompt_id]
        await asyncio.sleep(2.0)
    raise TimeoutError(f"prompt {prompt_id} did not finish in {timeout_s}s")


async def _download(client: httpx.AsyncClient, history_entry: dict[str, Any], dest: Path) -> Path:
    outputs = history_entry.get("outputs", {})
    for _, node_out in outputs.items():
        for img in node_out.get("images", []) or []:
            params = {
                "filename": img["filename"],
                "subfolder": img.get("subfolder", ""),
                "type": img.get("type", "output"),
            }
            r = await client.get(f"{COMFY_URL}/view", params=params)
            r.raise_for_status()
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(r.content)
            return dest
    raise RuntimeError("no image in history outputs")


async def _generate(positive: str, negative: str, seed: int, png_path: Path, backend: str) -> Path:
    if backend == "flux2":
        workflow = _build_flux_workflow(positive, negative, seed)
    elif backend == "z-image":
        workflow = _build_zimage_workflow(positive, negative, seed)
    else:
        raise ValueError(f"unknown backend: {backend!r}")
    async with httpx.AsyncClient(timeout=1800.0) as client:
        pid = await _submit(client, workflow)
        entry = await _poll(client, pid)
        return await _download(client, entry, png_path)


def _to_webp(png_path: Path, webp_path: Path) -> None:
    img = Image.open(png_path).convert("RGB")
    img.save(webp_path, "WEBP", quality=WEBP_QUALITY, method=6)
    png_path.unlink(missing_ok=True)


def generate_for(
    recipe: Recipe,
    rewritten: RewrittenPrompt,
    *,
    out_dir: Path = NAS_HERO,
    backend: str = BACKEND,
) -> Path:
    """Render hero webp to NAS (CDN-served), plus local mirror copy."""
    import shutil
    out_dir.mkdir(parents=True, exist_ok=True)
    LOCAL_MIRROR_HERO.mkdir(parents=True, exist_ok=True)
    # PNG temp lives in local mirror so a partial render never ends up on the
    # CDN under a half-written webp filename.
    png_path = LOCAL_MIRROR_HERO / f"{recipe.id}.png"
    webp_path = out_dir / f"{recipe.id}.webp"
    seed = _seed_for_recipe(recipe.id)
    print(f"  [{backend}] {recipe.id} seed={seed} :: {rewritten.scene_summary[:80]}")
    asyncio.run(_generate(rewritten.positive, rewritten.negative, seed, png_path, backend))
    _to_webp(png_path, webp_path)
    # Local mirror for offline render fallback.
    shutil.copyfile(webp_path, LOCAL_MIRROR_HERO / f"{recipe.id}.webp")
    return webp_path


def _write_hero_url_map(recipes: list[Recipe], out_dir: Path) -> None:
    """slug -> public URL map for the renderer to embed."""
    entries: dict[str, dict] = {}
    for r in recipes:
        webp = out_dir / f"{r.id}.webp"
        if webp.exists() and webp.stat().st_size > 4096:
            entries[r.id] = {
                "label": r.name,
                "url": f"{PUBLIC_BASE_HERO}/{r.id}.webp",
            }
    HERO_URL_MAP.parent.mkdir(parents=True, exist_ok=True)
    import json as _json
    HERO_URL_MAP.write_text(_json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"hero url map -> {HERO_URL_MAP}")


def generate_all(
    recipes: list[Recipe],
    *,
    out_dir: Path = NAS_HERO,
    backend: str = BACKEND,
    override: bool = False,
    refresh_prompts: bool = False,
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    client = _gemini_client()
    cache = _load_cache()

    done: list[Path] = []
    for r in recipes:
        webp = out_dir / f"{r.id}.webp"
        if not override and webp.exists() and webp.stat().st_size > 4096:
            print(f"  skip {r.id} (exists)")
            done.append(webp)
            continue
        rewritten = rewrite(r, client=client, cache=cache, refresh=refresh_prompts)
        try:
            done.append(generate_for(r, rewritten, out_dir=out_dir, backend=backend))
        except Exception as e:
            print(f"  FAIL {r.id}: {str(e)[:160]}", file=sys.stderr)
    _write_hero_url_map(recipes, out_dir)
    return done


def _comfy_health() -> bool:
    try:
        r = httpx.get(f"{COMFY_URL}/system_stats", timeout=3.0)
        return r.status_code == 200
    except Exception:
        return False


def main() -> None:
    p = argparse.ArgumentParser(description="Recipe image generator (Flux2 / Z-Image)")
    sub = p.add_subparsers(dest="cmd", required=True)

    one = sub.add_parser("one", help="generate one recipe image")
    one.add_argument("--id", required=True)
    one.add_argument("--backend", choices=["flux2", "z-image"], default=BACKEND)
    one.add_argument("--refresh-prompt", action="store_true")

    all_p = sub.add_parser("all", help="generate every recipe image")
    all_p.add_argument("--backend", choices=["flux2", "z-image"], default=BACKEND)
    all_p.add_argument("--override", action="store_true")
    all_p.add_argument("--refresh-prompts", action="store_true")

    args = p.parse_args()

    if not _comfy_health():
        print(
            f"ComfyUI not reachable at {COMFY_URL}. Start it with:\n"
            f"  cd ~/ComfyUI/ComfyUI && python main.py --listen 127.0.0.1 --port 8188 --lowvram",
            file=sys.stderr,
        )
        sys.exit(2)

    recipes = load_recipes(DATA_FILE)

    if args.cmd == "one":
        target = next((r for r in recipes if r.id == args.id), None)
        if not target:
            print(f"recipe id '{args.id}' not found. Available: {[r.id for r in recipes]}", file=sys.stderr)
            sys.exit(1)
        rewritten = rewrite(target, refresh=args.refresh_prompt)
        path = generate_for(target, rewritten, backend=args.backend)
        print(f"saved {path}")
    elif args.cmd == "all":
        paths = generate_all(
            recipes,
            backend=args.backend,
            override=args.override,
            refresh_prompts=args.refresh_prompts,
        )
        print(f"done: {len(paths)}/{len(recipes)}")


if __name__ == "__main__":
    main()
