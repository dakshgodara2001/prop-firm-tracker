# Deploying to propfirmsdealz.com (VPS + Docker + Caddy)

Public dashboard, automatic HTTPS. ~15 minutes end to end.

## 1. Create a VPS
Any Linux host works. Cheapest solid option: **Hetzner CX22** (~€4/mo) or
**DigitalOcean $6 droplet**.
- Image: **Ubuntu 24.04**
- Open firewall ports **22, 80, 443**
- Note the server's **public IPv4** (e.g. `203.0.113.45`)

## 2. Point DNS at the server
At your domain registrar (wherever you bought propfirmsdealz.com), add two
**A records** to that IP:

| Type | Name  | Value           | TTL |
|------|-------|-----------------|-----|
| A    | `@`   | `<SERVER_IP>`   | 3600 |
| A    | `www` | `<SERVER_IP>`   | 3600 |

Verify propagation before continuing (must return your IP):
```bash
dig +short propfirmsdealz.com
```

## 3. Install Docker on the server
SSH in (`ssh root@<SERVER_IP>`) and run:
```bash
curl -fsSL https://get.docker.com | sh
```

## 4. Get the code onto the server
```bash
git clone <YOUR_REPO_URL> /opt/prop-firm-tracker   # or scp the folder up
cd /opt/prop-firm-tracker
cp .env.example .env        # defaults already target propfirmsdealz.com
```

## 5. Launch
```bash
docker compose up -d --build
```
Caddy fetches HTTPS certs on first boot (needs DNS from step 2 already live
and ports 80/443 reachable). Watch it happen:
```bash
docker compose logs -f caddy     # look for "certificate obtained successfully"
```

## 6. Seed data
The scheduler will populate going forward, but backfill history now so the
dashboard isn't empty:
```bash
docker compose exec web python main.py refresh-masters --force
docker compose exec web python main.py backfill-prices --from-date 2026-05-01 --to-date 2026-07-06
docker compose exec web python main.py backfill --from-date 2026-06-30 --to-date 2026-07-06
```

## 7. Verify
```bash
curl -s https://propfirmsdealz.com/healthz     # {"status":"ok",...}
```
Then open **https://propfirmsdealz.com** in a browser.

---

## Operating it
- **Update after code changes:** `git pull && docker compose up -d --build`
- **Logs:** `docker compose logs -f web` / `caddy` / `scheduler`
- **Manual run:** `docker compose exec web python main.py run-daily`
- **Backups:** the SQLite DB + raw snapshots live in `./data` on the host —
  `tar czf backup.tgz data/` (or snapshot the volume).
- **Lock it down later:** uncomment the `basicauth` block in
  `deploy/Caddyfile`, set `BASIC_AUTH_USER`/`BASIC_AUTH_HASH` in `.env`
  (hash via `docker compose run --rm caddy caddy hash-password --plaintext 'pw'`),
  then `docker compose up -d`.
- **TLS certs** persist in the `caddy_data` volume — don't delete it, or you
  risk hitting Let's Encrypt rate limits on repeated re-issue.
