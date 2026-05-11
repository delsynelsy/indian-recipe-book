#!/usr/bin/env python3
"""
Indian Recipe Book — generator CLI

Usage:
  python generate.py                  # build output/index.html
  python generate.py --output FILE    # custom output path
  python generate.py validate         # check macro consistency
  python generate.py macros           # print nutrition summary
  python generate.py plan             # analyse meal plan feasibility
"""

import shutil
import sys
from pathlib import Path

import click
import markdown as md_lib
from rich.console import Console
from rich.table import Table
from rich import box

from src.parser import load_recipes, parse_meal_plan
from src.generator import generate_html
from src.macros import validate_all, nutrition_summary, plan_feasibility

ROOT = Path(__file__).parent
DATA_FILE = ROOT / "data" / "recipes.yaml"
MEAL_PLAN_FILE = ROOT / "meal_plan.md"
TEMPLATES_DIR = ROOT / "templates"
DEFAULT_OUTPUT = ROOT / "output" / "index.html"

console = Console()


@click.group(invoke_without_command=True)
@click.option("--output", "-o", default=str(DEFAULT_OUTPUT), show_default=True,
              help="Output HTML path.")
@click.pass_context
def cli(ctx, output):
    """Indian Recipe Book — static site generator."""
    if ctx.invoked_subcommand is None:
        _build(Path(output))


IMAGES_DIR = ROOT / "images"


def _swap_generated_images(recipes: list) -> None:
    """If images/<id>.webp exists, point Recipe.image.src at it."""
    for r in recipes:
        webp = IMAGES_DIR / f"{r.id}.webp"
        if webp.exists() and webp.stat().st_size > 4096:
            r.image.src = f"images/{r.id}.webp"


IMAG_REF_DIR = ROOT / "imag_references"


def _sync_assets(output_dir: Path) -> None:
    """Copy imag_references/ and images/ into the output directory so the
    HTML bundle is self-contained when opened as a file or deployed."""
    for src_dir in (IMAG_REF_DIR, IMAGES_DIR):
        if not src_dir.exists():
            continue
        dst = output_dir / src_dir.name
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src_dir, dst)


def _build(output: Path):
    recipes = load_recipes(DATA_FILE)
    _swap_generated_images(recipes)
    meal_plan_html = ""
    if MEAL_PLAN_FILE.exists():
        raw_md = MEAL_PLAN_FILE.read_text(encoding="utf-8")
        meal_plan_html = md_lib.markdown(
            raw_md,
            extensions=["tables", "nl2br", "sane_lists"],
        )
    generate_html(recipes, TEMPLATES_DIR, output, meal_plan_html)
    _sync_assets(output.parent)
    console.print(f"[bold green]✓[/] Generated [cyan]{len(recipes)}[/] recipes → [bold]{output}[/]")


@cli.command()
def validate():
    """Validate macro totals for all recipes."""
    recipes = load_recipes(DATA_FILE)
    issues = validate_all(recipes)
    if issues:
        console.print("[bold yellow]⚠  Macro warnings:[/]")
        for issue in issues:
            console.print(f"  [yellow]{issue}[/]")
        sys.exit(1)
    else:
        console.print(f"[bold green]✓[/] All [cyan]{len(recipes)}[/] recipes passed macro validation.")


@cli.command()
def macros():
    """Print average nutrition across all recipes."""
    recipes = load_recipes(DATA_FILE)
    s = nutrition_summary(recipes)

    table = Table(title="Nutrition Summary", box=box.SIMPLE_HEAVY)
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")
    table.add_row("Recipes loaded", str(s["count"]))
    table.add_row("Total kcal (all recipes)", f"{s['total_kcal']} kcal")
    table.add_row("Average kcal / recipe", f"{s['avg_kcal']:.0f} kcal")
    table.add_row("Average protein", f"{s['avg_protein_g']:.1f} g")
    table.add_row("Average carbs", f"{s['avg_carbs_g']:.1f} g")
    table.add_row("Average fat", f"{s['avg_fat_g']:.1f} g")
    console.print(table)

    for r in recipes:
        console.print(
            f"  [dim]{r.name:30s}[/] "
            f"[bold]{r.nutrition.kcal:>4}[/] kcal  "
            f"P[cyan]{r.nutrition.protein:>3}g[/]  "
            f"C[yellow]{r.nutrition.carbs:>3}g[/]  "
            f"F[magenta]{r.nutrition.fat:>3}g[/]"
        )


@cli.command("regen-images")
@click.option("--backend", type=click.Choice(["flux2", "z-image"]), default=None,
              help="Override IMG_BACKEND env (default flux2).")
@click.option("--override", is_flag=True, help="Regenerate even if webp exists.")
@click.option("--refresh-prompts", is_flag=True, help="Re-run Gemini prompt rewriter.")
@click.option("--id", "recipe_id", default=None, help="Just one recipe id (smoke test).")
def regen_images(backend, override, recipe_id, refresh_prompts):
    """Run Gemini prompt rewriter + ComfyUI image generation."""
    from src.image_gen import generate_for, generate_all, _comfy_health, BACKEND, COMFY_URL
    from src.prompt_rewriter import rewrite

    if not _comfy_health():
        console.print(
            f"[red]ComfyUI not reachable at {COMFY_URL}.[/] Start with:\n"
            f"  [dim]cd ~/ComfyUI/ComfyUI && python main.py --listen 127.0.0.1 --port 8188 --lowvram[/]"
        )
        sys.exit(2)

    chosen = backend or BACKEND
    recipes = load_recipes(DATA_FILE)

    if recipe_id:
        target = next((r for r in recipes if r.id == recipe_id), None)
        if not target:
            console.print(f"[red]recipe id '{recipe_id}' not found.[/] Available: {[r.id for r in recipes]}")
            sys.exit(1)
        rewritten = rewrite(target, refresh=refresh_prompts)
        path = generate_for(target, rewritten, backend=chosen)
        console.print(f"[bold green]✓[/] [cyan]{recipe_id}[/] → [bold]{path}[/]")
        return

    paths = generate_all(
        recipes,
        backend=chosen,
        override=override,
        refresh_prompts=refresh_prompts,
    )
    console.print(f"[bold green]✓[/] generated [cyan]{len(paths)}[/]/{len(recipes)} images (backend={chosen})")


@cli.command()
def cards():
    """Generate standalone vintage recipe cards → output/cards.html"""
    recipes = load_recipes(DATA_FILE)
    _swap_generated_images(recipes)
    output = ROOT / "output" / "cards.html"
    generate_html(recipes, TEMPLATES_DIR, output, template_name="cards.html.j2")
    _sync_assets(output.parent)
    console.print(f"[bold green]✓[/] Generated [cyan]{len(recipes)}[/] recipe cards → [bold]{output}[/]")


@cli.command()
def plan():
    """Analyse meal plan feasibility from meal_plan.md."""
    if not MEAL_PLAN_FILE.exists():
        console.print(f"[red]Not found:[/] {MEAL_PLAN_FILE}")
        sys.exit(1)

    profile = parse_meal_plan(MEAL_PLAN_FILE)
    result = plan_feasibility(profile)

    table = Table(title="Meal Plan Feasibility", box=box.SIMPLE_HEAVY)
    table.add_column("Property", style="bold")
    table.add_column("Value", justify="right")
    table.add_row("Current weight", f"{profile.current_weight} kg")
    table.add_row("Goal weight", f"{profile.goal_weight} kg")
    table.add_row("Weight to lose", f"{profile.weight_to_lose:.1f} kg")
    table.add_row("Height / Age", f"{profile.height} cm / {profile.age} yrs")
    table.add_row("Current BMI", str(profile.bmi_current))
    table.add_row("Goal BMI", str(profile.bmi_goal))
    table.add_row("Timeline", f"{profile.weeks} weeks")
    table.add_row("Calculated BMR", f"{result['calculated_bmr']} kcal/day")
    table.add_row("Calculated TDEE (light)", f"{result['calculated_tdee']} kcal/day")
    table.add_row("Required daily deficit", f"{result['required_daily_deficit']} kcal/day")
    table.add_row("Projected daily intake", f"{result['projected_daily_intake']} kcal/day")
    console.print(table)

    color = "green" if result["is_safe"] else "red"
    console.print(f"[bold {color}]{result['note']}[/]")


if __name__ == "__main__":
    cli()
