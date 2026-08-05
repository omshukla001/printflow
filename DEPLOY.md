# Deploying the PrintFlow server

## Why not Vercel

Vercel runs Python as short-lived serverless functions. This app cannot work there:

| Requirement | Vercel |
|---|---|
| 50 MB uploads (`MAX_UPLOAD_BYTES`) | request body capped at **4.5 MB** |
| Files kept until the agent fetches them | filesystem wiped every invocation |
| `libreoffice` for DOC/DOCX/TXT → PDF | cannot install a ~400 MB binary |
| Watchdog + CUPS poller background threads | frozen after each response |
| SSE streams held 120 s | function timeout, billed for the hold |
| SQLite / shared state | no shared writable disk |

Render, Railway and Fly.io run a **persistent container**, which satisfies all of these.

---

## Deploy on Render (recommended)

### 1. Push this folder to GitHub

```bash
git init && git add -A
git commit -m "PrintFlow server"
git remote add origin git@github.com:<you>/printflow.git
git push -u origin main
```

### 2. Create the services

In Render: **New → Blueprint**, pick the repo. It reads `render.yaml` and creates
the web service, a 5 GB disk at `/data`, and a Postgres database.

### 3. Fill in the two secrets

`render.yaml` marks these `sync: false`, so Render will prompt for them:

| Variable | Value |
|---|---|
| `AGENT_API_KEY` | Leave **blank** on a new deployment — kiosks enroll for their own keys (see below). Set it only if you have an existing hand-configured Pi to keep working. |
| `SITE_URL` | your Render URL, e.g. `https://printflow.onrender.com` |

`SITE_URL` is what the kiosk QR codes encode, so it must be the address phones
actually reach. `SECRET_KEY` is generated automatically, and `DATABASE_URL` is
wired from the database.

> Never commit a real key. `AGENT_API_KEY` is the legacy shared secret; new
> kiosks get per-device keys through enrollment and nothing sensitive needs to
> live in this repository.

### 4. Create your admin login

Once deployed, open the Render **Shell** tab:

```bash
python scripts/create_admin.py
```

It prompts for username, email, name and password.

### 5. Set up a kiosk

Nothing needs configuring by hand. In the admin UI, **Kiosks → Add kiosk** gives
you a one-time code. On the Raspberry Pi:

```bash
curl -sSL https://your-server/install.sh | sudo bash
```

It asks for the server URL and that code, then installs the packages, finds the
printer, enrolls for a key belonging to that device, writes it to
`/etc/printflow-agent.env` (mode 0600, root only), installs both systemd units
and the browser autostart, and starts everything. Non-interactively:

```bash
sudo bash install.sh --server https://your-server --code K7P-3RD-92X
```

Useful flags: `--user NAME` (which account runs the services), `--printer NAME`
(skip auto-detection), `--no-display` (print only, no screen).

Re-running is safe — it re-enrolls and upgrades in place.

**Why codes rather than a shared key.** Each kiosk ends up with its own
credential, so one can be revoked from **Kiosks → Revoke key** without touching
the others, and no long-lived secret has to be copied between machines. The code
itself is single-use and expires in 15 minutes (`ENROLL_CODE_MINUTES`).

Enrollment refuses to run over plain HTTP, since the code and the new key both
cross the wire. On a trusted LAN you can override that with
`ALLOW_INSECURE_ENROLL=true` on the server.

### 6. Adopting an existing hand-configured Pi

A Pi already running with the shared `AGENT_API_KEY` keeps working — that key is
still accepted. To move it onto its own credential, just run the installer on it;
it will re-enroll and overwrite the old configuration. Once every kiosk is
enrolled, clear `AGENT_API_KEY` on the server so the shared secret is gone.

---

## How long files live on the server

The server is a staging post, not an archive. A document sits on the mounted
disk only between the upload and the moment the Pi confirms the print:

| Job outcome | When the file is deleted |
|---|---|
| Completed | Immediately, when the agent reports the print finished |
| Cancelled | Immediately |
| Failed | After `FAILED_FILE_RETENTION_HOURS` (24) — so an admin can retry |
| Queued, then abandoned | After `ABANDONED_FILE_HOURS` (72) — job is cancelled, any charge refunded |

The `print_jobs` row survives with `files_purged_at` set, so history, receipts
and the payment ledger are unaffected — only the bytes go. On the Pi side the
agent deletes its own cached copy from `TEMP_DIR` as soon as CUPS reports the
job finished, and sweeps leftovers from a crashed run at startup.

Two knobs are worth knowing:

- `FILE_RETENTION_MINUTES` (default `0`) — a grace period after completion.
  At `0`, **one-click reprint stops working** for printed jobs: the user gets
  "upload it again to reprint". Set it to `60` to keep reprint alive for an
  hour after printing.
- `PURGE_AFTER_PRINT=false` — disables all of this and keeps every upload
  forever. Only sensible if you have deliberately sized the disk for it.

A watchdog sweep runs every 15 minutes to catch anything the immediate delete
could not take: expired grace periods, failed-job windows, and files still
shared with a live reprint.

---

## Running cost (Render, approximate)

| Item | ~USD/month |
|---|---|
| Web service, Starter | 7 |
| Postgres, basic-256mb | 6 |
| 5 GB disk | 1.25 |

The free tier will **not** work: no persistent disk, and the service sleeps when
idle so the agent's polls would hit a cold container.

---

## Deploy on AWS EC2

One instance runs the whole server. The Pi keeps printing; it just dials out to
EC2 instead of to a machine on the shop LAN.

### 1. Launch the instance

| Setting | Value |
|---|---|
| AMI | Ubuntu Server 24.04 LTS |
| Type | `t3.small` (2 GB RAM) — LibreOffice needs the headroom, `t3.micro` will OOM on a DOCX |
| Storage | 20 GB gp3 |
| Security group | 22 from **your IP only**, 80 and 443 from `0.0.0.0/0` |

Give it an **Elastic IP**. Without one the address changes on every stop/start,
and the kiosk QR codes — which encode `SITE_URL` — would all break.

### 2. Point a domain at it

You need a real hostname, not the raw IP. Kiosk enrollment **refuses to run over
plain HTTP** (`config.py`, `ALLOW_INSECURE_ENROLL`), and you cannot get a
certificate for an IP address. Add an A record for e.g. `print.yourshop.com`
pointing at the Elastic IP.

### 3. Install Docker and clone

```bash
ssh -i your-key.pem ubuntu@print.yourshop.com

sudo apt update && sudo apt install -y docker.io docker-compose-v2 git
sudo usermod -aG docker ubuntu && newgrp docker

git clone https://github.com/omshukla001/printflow.git
cd printflow
```

### 4. Write the environment file

```bash
PGPW=$(openssl rand -hex 16)
cat > .env <<EOF
SECRET_KEY=$(openssl rand -hex 32)
SITE_URL=https://print.yourshop.com
CLOUD_MODE=true
FLASK_ENV=production
DATA_DIR=/data
POSTGRES_PASSWORD=$PGPW
DATABASE_URL=postgresql://printflow:$PGPW@db:5432/printflow
PAPER_SIZES=A4
COLOR_PRINTING_ENABLED=false
EOF
chmod 600 .env
```

`POSTGRES_PASSWORD` and the password inside `DATABASE_URL` must match — the
snippet above generates one value and uses it in both. `.env` is gitignored;
keep it that way.

`SECRET_KEY` is mandatory — the app refuses to boot in production with the
default (`Config.validate`). `SITE_URL` must be the **https** address, because
it is what the kiosk QR encodes and what the phone will open.

### 5. Bring it up

`compose.yaml` in the repo root runs the app image alongside Postgres, with a
named volume at `/data` for uploads, previews and receipts:

```bash
docker compose up -d --build
docker compose logs -f app        # watch it migrate its own schema and start
```

Create your admin login:

```bash
docker compose exec app python scripts/create_admin.py
```

### 6. TLS in front

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
sudo tee /etc/nginx/sites-available/printflow >/dev/null <<'EOF'
server {
    listen 80;
    server_name print.yourshop.com;
    client_max_body_size 60M;          # uploads are capped at 50 MB
    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;           # SSE job status must stream, not buffer
        proxy_read_timeout 200s;       # streams are held up to 120s
    }
}
EOF
sudo ln -sf /etc/nginx/sites-available/printflow /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d print.yourshop.com
```

`client_max_body_size` and `proxy_buffering off` are not optional. Without the
first, a 50 MB upload dies at nginx with a 413 before Flask sees it. Without the
second, nginx holds the SSE stream and the customer's job status never updates.

### 7. Repoint the Pi at EC2

On the Raspberry Pi, edit the agent's server URL and restart:

```bash
sudo systemctl edit printflow-agent      # set Environment=SERVER_URL=https://print.yourshop.com
sudo systemctl restart printflow-agent
journalctl -u printflow-agent -f         # expect heartbeats, not connection-refused
```

Then in the admin UI, **Kiosks → Add kiosk** for a one-time code, and enroll the
Pi against the new server. Each kiosk gets its own key, so the old one can be
revoked without touching anything else.

### 8. Redeploying later

```bash
cd printflow && git pull && docker compose up -d --build
```

The schema migrates itself at startup — there is no separate migration step.

### Running cost (ap-south-1, approximate)

| Item | USD / month |
|---|---|
| t3.small on-demand | 15 |
| 20 GB gp3 | 1.60 |
| Elastic IP (while attached) | 0 |

A 1-year Compute Savings Plan takes the instance to roughly $9. RDS instead of
the Postgres container adds about $15 — worth it for automated backups once the
shop has real billing history in there.

---

## Railway / Fly.io instead

The `Dockerfile` is portable; only the orchestration differs.

- **Railway** — New Project → Deploy from repo. Add a Postgres plugin (injects
  `DATABASE_URL`), attach a Volume mounted at `/data`, and set the same env vars
  as in `render.yaml`.
- **Fly.io** — `fly launch` (detects the Dockerfile), then
  `fly volumes create printflow_data --size 5`, mount it at `/data` in
  `fly.toml`, `fly postgres create && fly postgres attach`, and
  `fly secrets set AGENT_API_KEY=... SITE_URL=...`.

---

## What changed for deployment

Application logic was not modified. The additions:

| File | Purpose |
|---|---|
| `Dockerfile` | Python 3.13 + LibreOffice + libmagic |
| `requirements-server.txt` | server deps; **drops `pycups`**, adds `psycopg2-binary` |
| `gunicorn.server.conf.py` | 1 worker, `gthread`, 16 threads |
| `render.yaml` | Render blueprint |
| `.dockerignore` | keeps venvs and local data out of the image |
| `config.py` | `DATA_DIR` for the mounted volume; `postgres://` → `postgresql://` |

Three of those deserve explanation:

**`pycups` is dropped.** With `CLOUD_MODE=true` every
`from app.services.printer import ...` sits behind a cloud-mode branch, so the
`import cups` at `app/services/printer.py:2` never runs. Keeping it would also
fail the image build, since it needs CUPS headers.

**One worker, not two.** `create_app()` starts an APScheduler watchdog and a pool
of preview threads *per process*. The Pi's `gunicorn.conf.py` uses
`workers = 2`, which means two schedulers running `sweep_stuck_jobs` over the
same rows. The server config uses a single worker and gets concurrency from
threads instead.

**`gthread`, not `sync`.** The live-status endpoint holds an SSE connection open
for up to 120 s (`app/services/sse.py`). Under `sync` workers each open stream
occupies a whole worker, so a couple of users watching their jobs would block
the entire site.

The `postgres://` rewrite matters because managed Postgres still hands out that
legacy scheme and SQLAlchemy 1.4+ refuses to load a dialect for it. Pool sizing
is applied only on Postgres — SQLite's pool class rejects those arguments, and
the Pi still runs on SQLite.
