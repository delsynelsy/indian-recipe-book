"""Tests for src/quantities.py — Spanish ingredient-line parsing (WP1)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


from src.quantities import (
    NOUN_PLURAL,
    parse_servings,
    parse_ingredient,
    structure_ingredients,
)


class TestParseServings(unittest.TestCase):
    def test_two_porciones(self):
        self.assertEqual(parse_servings("2 porciones"), (2, "porciones"))

    def test_singular_porcion_normalized_to_plural(self):
        self.assertEqual(parse_servings("1 porción"), (1, "porciones"))

    def test_chillas_unit(self):
        self.assertEqual(parse_servings("4 chillas"), (4, "chillas"))

    def test_parens_annotation_ignored(self):
        self.assertEqual(parse_servings("2 porciones (4 parathas)"), (2, "porciones"))

    def test_no_number_defaults_to_two(self):
        self.assertEqual(parse_servings("al gusto"), (2, "porciones"))


class TestParseIngredient(unittest.TestCase):
    def test_unit_int(self):
        d = parse_ingredient("1 taza dal moong partido amarillo")
        self.assertEqual(d["kind"], "unit")
        self.assertEqual(d["q"], (1, 1))
        self.assertIsNone(d["q2"])
        self.assertEqual(d["u1"], "taza")
        self.assertEqual(d["un"], "tazas")
        self.assertEqual(d["r1"], "dal moong partido amarillo")
        self.assertEqual(d["rn"], "dal moong partido amarillo")
        self.assertEqual(d["pre"], "")

    def test_unit_fraction(self):
        d = parse_ingredient("1/2 cdta comino molido")
        self.assertEqual(d["kind"], "unit")
        self.assertEqual(d["q"], (1, 2))
        self.assertEqual(d["u1"], "cdta")

    def test_unit_decimal(self):
        d = parse_ingredient("1.5 tazas agua caliente")
        self.assertEqual(d["q"], (3, 2))
        self.assertEqual(d["u1"], "taza")
        self.assertEqual(d["un"], "tazas")

    def test_unit_decimal_comma(self):
        d = parse_ingredient("1,5 tazas agua")
        self.assertEqual(d["q"], (3, 2))

    def test_unit_grams_invariant(self):
        d = parse_ingredient("300 g pechuga de pollo sin hueso")
        self.assertEqual(d["q"], (300, 1))
        self.assertEqual(d["u1"], "g")
        self.assertEqual(d["un"], "g")

    def test_unit_dientes_de(self):
        d = parse_ingredient("4 dientes de ajo finamente picados")
        self.assertEqual(d["kind"], "unit")
        self.assertEqual(d["q"], (4, 1))
        self.assertEqual(d["u1"], "diente")
        self.assertEqual(d["un"], "dientes")
        self.assertEqual(d["r1"], "ajo finamente picados")

    def test_count_noun(self):
        d = parse_ingredient("2 tomates medianos triturados")
        self.assertEqual(d["kind"], "count")
        self.assertEqual(d["q"], (2, 1))
        self.assertEqual(d["u1"], "")
        self.assertEqual(d["r1"], "tomate medianos triturados")
        self.assertEqual(d["rn"], "tomates medianos triturados")

    def test_count_fraction_noun(self):
        d = parse_ingredient("1/2 cebolla finamente picada")
        self.assertEqual(d["kind"], "count")
        self.assertEqual(d["q"], (1, 2))
        self.assertEqual(d["r1"], "cebolla finamente picada")
        self.assertEqual(d["rn"], "cebollas finamente picada")

    def test_count_range(self):
        d = parse_ingredient("8–10 hojas de curry")
        self.assertEqual(d["kind"], "count")
        self.assertEqual(d["q"], (8, 1))
        self.assertEqual(d["q2"], (10, 1))
        self.assertEqual(d["r1"], "hoja de curry")
        self.assertEqual(d["rn"], "hojas de curry")

    def test_mid_zumo_fraction(self):
        d = parse_ingredient("Zumo de 1/2 lima")
        self.assertEqual(d["kind"], "mid")
        self.assertEqual(d["pre"], "Zumo de ")
        self.assertEqual(d["q"], (1, 2))
        self.assertEqual(d["r1"], "lima")
        self.assertEqual(d["rn"], "limas")

    def test_mid_zumo_int(self):
        d = parse_ingredient("Zumo de 1 lima")
        self.assertEqual(d["kind"], "mid")
        self.assertEqual(d["q"], (1, 1))

    def test_prose_sal(self):
        d = parse_ingredient("Sal al gusto")
        self.assertEqual(d["kind"], "prose")
        self.assertIsNone(d["q"])
        self.assertEqual(d["r1"], "Sal al gusto")

    def test_prose_oil(self):
        d = parse_ingredient("Aceite de oliva para cocinar")
        self.assertEqual(d["kind"], "prose")
        self.assertIsNone(d["q"])

    def test_prose_pizca(self):
        d = parse_ingredient("Pizca de sal")
        self.assertEqual(d["kind"], "prose")


class TestNounPluralMap(unittest.TestCase):
    def test_known_nouns_cover_both_directions(self):
        for sing, plur in NOUN_PLURAL.items():
            self.assertEqual(NOUN_PLURAL.get(plur), sing, f"{plur} must map back to {sing}")

    def test_servings_words_present(self):
        for w in ("porción", "porciones", "chilla", "chillas", "dosa", "dosas",
                  "idli", "idlis", "paratha", "parathas"):
            self.assertIn(w, NOUN_PLURAL, f"{w} missing from NOUN_PLURAL")

    def test_data_countables_present(self):
        for w in ("cebolla", "cebollas", "tomate", "tomates", "chile", "chiles",
                  "hoja", "hojas", "clavo", "clavos", "zanahoria", "zanahorias",
                  "dátil", "dátiles", "almendra", "almendras", "rama", "ramas",
                  "vaina", "vainas", "pimiento", "pimientos", "lima", "limas",
                  "diente", "dientes"):
            self.assertIn(w, NOUN_PLURAL)


def _recipe(servings="2 porciones", ingredients=None, id="t"):
    from src.models import NutritionInfo, Recipe, RecipeImage
    return Recipe(
        id=id, name="T", subtitle="s", phase="Fase 1", meal="Comida",
        type="Vegetariano", prep="5 min", cook="10 min", servings=servings,
        nutrition=NutritionInfo(kcal=100, protein=5, carbs=10, fat=3),
        image=RecipeImage(src="x.webp", css_class="img-x"),
        ingredients=ingredients or [], steps=[], tip="",
    )


class TestStructureIngredients(unittest.TestCase):
    def test_index_aligned_and_js_shape(self):
        r = _recipe(ingredients=[
            "1 taza dal moong",
            "8–10 hojas de curry",
            "Sal al gusto",
        ])
        out = structure_ingredients(r)
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0]["q"], [1, 1])       # lists, JSON-ready
        self.assertEqual(out[1]["q2"], [10, 1])
        self.assertIsNone(out[2]["q"])              # prose: q None marks unscaled
        self.assertEqual(out[2]["r1"], "Sal al gusto")
        for d in out:
            self.assertEqual(
                sorted(d.keys()),
                ["pre", "q", "q2", "r1", "rn", "u1", "un"],
            )


class TestRecipeToJs(unittest.TestCase):
    def test_emits_sbase_sunit_ing(self):
        from src.generator import _recipe_to_js
        r = _recipe(
            servings="2 porciones (4 chillas)",
            ingredients=["1/2 cdta sal"],
        )
        js = _recipe_to_js(r)
        self.assertEqual(js["sBase"], 2)
        self.assertEqual(js["sUnit"], "porciones")
        self.assertEqual(len(js["ing"]), 1)
        self.assertEqual(js["ing"][0]["q"], [1, 2])
        # existing keys unchanged
        self.assertEqual(js["servings"], "2 porciones (4 chillas)")
        self.assertEqual(js["ingredients"], ["1/2 cdta sal"])


if __name__ == "__main__":
    unittest.main()


class TestCoverage(unittest.TestCase):
    def test_coverage_counts_and_flags(self):
        from src.quantities import coverage
        rs = [
            _recipe(id="a", ingredients=[
                "1 taza arroz", "Sal al gusto"]),
            _recipe(ingredients=["8–10 hojas de curry", "2 tomates", "1 cda ghee"]),
        ]
        cov = coverage(rs)
        self.assertEqual(cov["total"], 5)
        self.assertEqual(cov["scalable"], 4)
        self.assertEqual(cov["kinds"]["unit"], 2)
        self.assertEqual(cov["kinds"]["count"], 2)
        self.assertEqual(cov["kinds"]["prose"], 1)
        self.assertEqual(cov["unmapped"], [])
        self.assertEqual(cov["bad_servings"], [])

    def test_coverage_flags_unmapped_noun_and_bad_servings(self):
        from src.quantities import coverage
        rs = [
            _recipe(servings="0 porciones", ingredients=["3 platanoanos"]),
        ]
        cov = coverage(rs)
        self.assertEqual(cov["unmapped"], [("t", "3 platanoanos")])
        self.assertEqual(cov["bad_servings"], ["t"])
        self.assertEqual(cov["scalable"], 1)
        self.assertEqual(cov["total"], 1)
