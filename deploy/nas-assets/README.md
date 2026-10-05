# recipe-book on nas-bitcorp: runbook

Two nginx containers in one compose project (`deploy`), both published through
the "Portfolio" Cloudflare tunnel:

| Service | NAS port | Public URL | Serves |
|---|---|---|---|
| `recipe-assets` | 8182 | https://images.mohammadasjad.com | `/volume1/projects/recipe-book/assets` (`hero/`, `steps/`, `ingredients/`) |
| `recipe-site` | 8183 | https://recipes.mohammadasjad.com | `/volume1/projects/recipe-book/site` (built `output/`) |

The compose dir on the NAS is `/volume1/projects/recipe-book/deploy` (this
folder's `docker-compose.yml`, `nginx.conf`, `site.conf`). The dev box mounts
`//192.168.0.13/projects` at `/mnt/nas`, which is where the image generators
write (`/mnt/nas/recipe-book/assets/...`).

## Publish the site

```bash
deploy/publish.sh   # clean build (index + cards) -> privacy guard -> tar over ssh -> smoke check
```

## Change compose or nginx config

```bash
tar czf - -C deploy/nas-assets docker-compose.yml nginx.conf site.conf \
  | ssh nas-bitcorp 'tar xzf - -C /volume1/projects/recipe-book/deploy'
ssh nas-bitcorp 'cd /volume1/projects/recipe-book/deploy \
  && /usr/local/bin/docker run --rm -v "$PWD/site.conf:/etc/nginx/conf.d/default.conf:ro" nginx:1.27-alpine nginx -t \
  && /usr/local/bin/docker compose up -d \
  && /usr/local/bin/docker compose restart recipe-assets recipe-site'
```

Gotchas:
- `docker` is not on PATH in non-interactive ssh; call `/usr/local/bin/docker`.
- Configs are single-file bind mounts. tar replaces the file (new inode), and
  only a container restart picks it up; `up -d` and `nginx -s reload` do not.
- rsync to this NAS is flaky; tar-over-ssh is the proven path.
- Create content dirs as `mohammad` before `up -d`, or Docker creates them
  root-owned and publishing can no longer write.
- `add_header` only fires on 2xx/3xx unless it has `always`, and any
  `add_header` in a location discards the server-level ones.
- The zone's Browser Cache TTL (4h) rewrites any lower `max-age` that browsers
  see. Cloudflare's edge still honours the origin value (a CDN 404 expires
  after 60s).

## Cloudflare (one-time, done 2026-10-05)

The Portfolio tunnel `ba4ddab0-d60f-4c6b-b243-2789d65c9300` (account
`fc710621b4feb656cd22c08d3c66ff83`) is remotely managed: ingress lives in
Cloudflare, not in a local `config.yml`. Its cloudflared runs on the NAS with
host networking, so services are `http://localhost:<port>`. Tokens come from
`~/.zshrc`. Never echo them.

Add a hostname: GET, insert before the catch-all, assert, PUT. The PUT replaces
the whole config, so round-trip everything else verbatim (incl. `warp-routing`
and the `path` rule on `expenses`).

```bash
zsh -c 'source ~/.zshrc >/dev/null 2>&1
U=https://api.cloudflare.com/client/v4/accounts/fc710621b4feb656cd22c08d3c66ff83/cfd_tunnel/ba4ddab0-d60f-4c6b-b243-2789d65c9300/configurations
curl -s -H "Authorization: Bearer $CLOUDFLARE_TUNNEL_EDIT_TOKEN" "$U" > ingress-before.json
jq ".result.config | .ingress |= (.[:-1] + [{hostname: \"recipes.mohammadasjad.com\", service: \"http://localhost:8183\"}] + .[-1:]) | {config: .}" ingress-before.json > ingress-after.json
jq -e ".config.ingress[-1].service == \"http_status:404\"" ingress-after.json
curl -s -X PUT -H "Authorization: Bearer $CLOUDFLARE_TUNNEL_EDIT_TOKEN" -H "Content-Type: application/json" --data @ingress-after.json "$U" | jq "{success, version: .result.version}"'
```

DNS: a proxied CNAME to the tunnel, in zone `3f5d0c88c39c69a5c12afae8ca70f906`.

```bash
zsh -c 'source ~/.zshrc >/dev/null 2>&1
curl -s -X POST -H "Authorization: Bearer $CLOUDFLARE_API" -H "Content-Type: application/json" \
  --data "{\"type\":\"CNAME\",\"name\":\"recipes\",\"content\":\"ba4ddab0-d60f-4c6b-b243-2789d65c9300.cfargotunnel.com\",\"proxied\":true,\"ttl\":1}" \
  https://api.cloudflare.com/client/v4/zones/3f5d0c88c39c69a5c12afae8ca70f906/dns_records | jq "{success, id: .result.id}"'
```

Don't use `~/bitcorp/deploy/cf-ingress.sh add` for this tunnel. It appends the
rule after the catch-all and sends `service` as an object, so it refuses to apply.

## Rollback

- Tunnel: PUT `{config: .result.config}` taken from `ingress-before.json`.
- DNS: `DELETE /zones/3f5d0c88c39c69a5c12afae8ca70f906/dns_records/<id>`.
- NAS: `docker compose rm -sf recipe-site`; restore `nginx.conf.bak-20261005` and
  restart `recipe-assets`.

## Generating images

```bash
cd ~/projects/indian-recipe-book
python -m src.ingredient_gen rewrite         # Gemini prompts (cached)
python -m src.ingredient_gen gen             # ingredient icons via Flux2
# files appear at /mnt/nas/recipe-book/assets/ingredients/<slug>.webp
```

`data/ingredient_image_map.json` maps each ingredient label to its public URL
under `https://images.mohammadasjad.com/ingredients/<slug>.webp`. The Jinja
template reads this map to embed icons.
