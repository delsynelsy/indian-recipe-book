#!/usr/bin/env bash
# Build the site and publish it to https://recipes.mohammadasjad.com
# (nginx `recipe-site` on nas-bitcorp; runbook: deploy/nas-assets/README.md).
set -euo pipefail
cd "$(dirname "$0")/.."

rm -rf output
python3 generate.py
python3 generate.py cards   # the "Tarjetas" tab iframes cards.html

# Trust boundary: no personal meal-plan stats, no third-party reference photos.
# A surviving marker means a malformed/unclosed private block leaked its content.
if grep -qE '>Peso actual<|Seguimiento del progreso esperado|private ?-->' output/*.html || [ -e output/imag_references ]; then
  echo "publish: private stats or reference photos in output/, refusing to ship" >&2
  exit 1
fi

# ponytail: overwrites in place (no downtime); files deleted locally linger on
# the NAS. No swap-by-rename: the container's bind mount pins the directory inode.
tar czf - -C output . | ssh nas-bitcorp \
  'mkdir -p /volume1/projects/recipe-book/site && tar xzf - -C /volume1/projects/recipe-book/site'

for page in "" cards.html; do
  curl -fsS -o /dev/null -w "recipes.mohammadasjad.com/$page -> %{http_code}\n" "https://recipes.mohammadasjad.com/$page"
done
