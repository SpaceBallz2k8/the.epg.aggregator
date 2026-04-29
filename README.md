# EPG App — Docker Setup

Everything runs in a single `docker compose` stack: the Flask app and a
PostgreSQL database, both on the same host. No reverse proxy, no Redis,
no external services required.

---

## Quick start

### 1. Edit your `.env` file

The `.env` file in this directory controls all configuration.
Open it and change at minimum:

| Variable | What it is |
|---|---|
| `POSTGRES_PASSWORD` | Database password (used internally) |
| `SECRET_KEY` | Flask session secret — **make this long and random** |
| `WHITELIST_IPS` | Comma-separated IPs that will never be auto-banned |
| `APP_PORT` | Host port the app listens on (default `5000`) |

To generate a good `SECRET_KEY`:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

### 2. Build and start

```bash
docker compose up -d --build
```

The first build takes a minute or two to download the base images and
install Python dependencies. Subsequent starts are fast.

### 3. Access the app

Open `http://<your-host-ip>:<APP_PORT>` in your browser.

Default admin credentials (set in app.py, change after first login):
- **Username:** `admin`
- **Password:** `admin123`

---

## Useful commands

| Task | Command |
|---|---|
| Start | `docker compose up -d` |
| Stop | `docker compose down` |
| View logs | `docker compose logs -f app` |
| Rebuild after code change | `docker compose up -d --build` |
| Open a psql shell | `docker compose exec db psql -U xmltv xmltv` |

---

## Data persistence

PostgreSQL data is stored in a Docker named volume (`postgres_data`).
It survives `docker compose down` and is only removed if you run:

```bash
docker compose down -v   # WARNING: deletes all database data
```

---

## Updating WHITELIST_IPS

Edit the `WHITELIST_IPS` line in `.env` (comma-separated, no spaces around
commas), then restart the app container:

```bash
docker compose up -d
```

---

## Migrating from the old setup

The `docker-entrypoint.sh` script automatically runs `migrate.py` on every
container start. Migrations are idempotent (uses `IF NOT EXISTS` / `ADD
COLUMN IF NOT EXISTS`) so running them repeatedly is safe.
