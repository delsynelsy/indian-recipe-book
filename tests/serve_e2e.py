"""E2E-only server: built output/ static + recipe-api under the same origin,
mirroring the production /api/ path proxy. Test infrastructure, never shipped.

    DB_PATH=/tmp/e2e.db RECIPE_EMAIL=e2e@example.com RECIPE_PASSWORD=e2epass123 \
      <venv>/uvicorn tests.serve_e2e:app --port 8902
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "deploy" / "nas-assets" / "api"))

from fastapi import FastAPI  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

import main  # noqa: E402

main.init_db()  # routes are copied below; main.app's lifespan does not come along

app = FastAPI()
for _route in main.app.routes:  # /api/... paths, absolute
    if _route.path.startswith("/api"):
        app.router.routes.append(_route)
app.mount("/", StaticFiles(directory=str(ROOT / "output"), html=True), name="static")
