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

import sys
from pathlib import Path

import click
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


def _build(output: Path):
    recipes = load_recipes(DATA_FILE)
    generate_html(recipes, TEMPLATES_DIR, output)
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
