from dataclasses import dataclass, field


@dataclass
class NutritionInfo:
    kcal: int
    protein: int
    carbs: int
    fat: int


@dataclass
class Step:
    icon: str
    action: str
    text: str
    time: str = ""


@dataclass
class RecipeImage:
    src: str
    css_class: str


@dataclass
class Recipe:
    id: str
    name: str
    subtitle: str
    phase: str
    meal: str
    type: str
    prep: str
    cook: str
    servings: str
    nutrition: NutritionInfo
    image: RecipeImage
    ingredients: list
    steps: list
    tip: str
    highlight_ingredients: list = field(default_factory=list)
    prep_note: str = ""

    @property
    def tags(self) -> list:
        return [self.phase, self.meal, self.type]


@dataclass
class MealPlanProfile:
    current_weight: float
    goal_weight: float
    height: float
    age: int
    bmr: float
    tdee_min: float
    tdee_max: float
    daily_target_min: int
    daily_target_max: int
    deficit_min: int
    deficit_max: int
    weeks: int

    @property
    def weight_to_lose(self) -> float:
        return self.current_weight - self.goal_weight

    @property
    def bmi_current(self) -> float:
        h = self.height / 100
        return round(self.current_weight / (h * h), 1)

    @property
    def bmi_goal(self) -> float:
        h = self.height / 100
        return round(self.goal_weight / (h * h), 1)
