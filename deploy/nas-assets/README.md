# recipe-assets — static image server for NAS

Serves generated recipe + ingredient illustrations at
`https://images.mohammadasjad.com/`.

## Layout on NAS

```
/volume1/recipe-book/assets/
  hero/        <- (planned) hero card images
  ingredients/ <- written by `python -m src.ingredient_gen gen`
```

The dev box reaches the same files via `/mnt/nas/recipe-book/assets/` (NFS/SMB
mount), and `src/ingredient_gen.py` writes there directly.

## First-time deploy

```bash
# 1. on dev box: copy deploy bundle to NAS
rsync -av deploy/nas-assets/ nas-bitcorp:/volume1/docker/recipe-assets/

# 2. ensure assets path exists on NAS
ssh nas-bitcorp 'mkdir -p /volume1/recipe-book/assets/ingredients'

# 3. start nginx container on NAS
ssh nas-bitcorp 'cd /volume1/docker/recipe-assets && docker compose up -d'

# 4. verify LAN reachability
curl -I http://192.168.0.13:8182/healthz
```

## Cloudflare tunnel route

Append to `~/.cloudflared/config.yml` on dev box (or whichever host runs the
tunnel `66e4fc41-0b59-41ff-a850-e4750fb26c9d`), BEFORE the `http_status:404`
catch-all:

```yaml
  - hostname: images.mohammadasjad.com
    service: http://192.168.0.13:8182
```

Then:

```bash
cloudflared tunnel route dns 66e4fc41-0b59-41ff-a850-e4750fb26c9d images.mohammadasjad.com
# SIGHUP the running tunnel to reload config
pkill -HUP -f 'cloudflared.*tunnel.*run'
# verify
curl -I https://images.mohammadasjad.com/healthz
```

## Generating images

```bash
cd ~/projects/indian-recipe-book
python -m src.ingredient_gen rewrite         # Gemini prompts (cached)
python -m src.ingredient_gen gen             # all 23 ingredients via Flux2
# files appear at /mnt/nas/recipe-book/assets/ingredients/<slug>.webp
```

`data/ingredient_image_map.json` maps each ingredient label to its public URL
under `https://images.mohammadasjad.com/ingredients/<slug>.webp`. The Jinja
template reads this map to embed icons.
