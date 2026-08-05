# PrintFlow

A self-service printing system for a print shop. Customers upload from their
phone, walk up to the kiosk, scan a QR code, and their document prints.

The shop side handles the rest: queue, pricing, billing, discounts, paper and
toner stock, and what every print actually costs.

---

## How it works

There are two programs. They are in this one repository because they are two
halves of one system, but they run on different machines.

```
   Customer's phone                Server (cloud)                Raspberry Pi
  ┌────────────────┐          ┌───────────────────┐          ┌────────────────┐
  │ upload a file  │─────────▶│ accounts, queue,  │          │ print agent    │
  │ pick options   │          │ pricing, billing  │◀─────────│ polls for work │
  │                │          │ offers, stock     │  every   │ prints via CUPS│
  │ scan the QR ───┼─────────▶│ marks job ready   │   2s     │ confirms done  │
  └────────────────┘          └───────────────────┘          ├────────────────┤
                                        ▲                    │ kiosk screen   │
                                        └────────────────────│ QR + slideshow │
                                             every 1.5s      └────────────────┘
```

**The Pi never accepts an incoming connection.** It dials out to the server and
nothing else. That is what lets it sit behind a shop's NAT or a 4G hotspot with
no port forwarding, no static IP, no certificate and no inbound firewall hole.
The cost is up to ~2s before a job starts printing, which nobody notices.

### A job, end to end

1. Customer uploads a PDF, image or Word document and picks copies, sides,
   page ranges and so on. The server counts pages, renders previews and quotes
   a price with any discounts already applied.
2. The job sits `queued`.
3. At the shop, the customer scans the kiosk QR with their phone. The server
   validates that (rotating, single-use) token and flips their jobs to
   `ready_to_print`.
4. The Pi's next poll claims the job, downloads the file, and submits it to CUPS.
5. When CUPS confirms, the Pi reports back. The server charges the customer,
   deducts paper and toner, records what the job cost the shop — and **deletes
   the stored document**.

---

## What it does

### For customers
- Upload PDF, PNG, JPG, DOC, DOCX or TXT — Office formats are converted to PDF
- Page previews before committing
- Copies, single/double sided, page ranges, odd/even, N-up, orientation,
  quality, collation; saved presets for repeat jobs
- Live price that matches what is actually charged
- QR check-in at the kiosk, job history, PDF receipts

### For the shop
- **Queue** with drag-to-reorder, priority for customers who have checked in
- **Pricing** per paper size and colour mode, with separate duplex rates and a
  full change history
- **Offers** — bulk discount tiers (25+ pages 10% off, and so on) and a referral
  programme where the invited friend and the inviter both get a voucher
- **Stock** — paper in sheets, toner in pages of cartridge yield, with low-stock
  warnings and a full movement log
- **Costing** — what each job cost in paper and ink, next to what was charged,
  with margin per job and per sheet
- **Kiosks** — every Pi's MAC, IP, hostname, state (idle / printing / needs
  attention / offline), with warnings when one drops off
- **Kiosk slides** — images, video, PDFs, custom HTML or an embedded web page
- **Billing** — running balance per customer, payments and refunds, audit log

---

## Running the server

Any host that runs a persistent container works. Render is what the blueprint
targets; Railway and Fly.io need only different orchestration.

```bash
git clone https://github.com/omshukla001/printflow.git
cd printflow
python3 -m venv venv && venv/bin/pip install -r requirements.txt
venv/bin/python scripts/create_admin.py
venv/bin/python run.py
```

For AWS EC2, `compose.yaml` runs the app and Postgres on one instance:

```bash
git clone https://github.com/omshukla001/printflow.git && cd printflow
# write .env (see DEPLOY.md), then:
docker compose up -d --build
docker compose exec app python scripts/create_admin.py
```

For Render, `render.yaml` is a one-click blueprint: web service, 5 GB disk at
`/data`, and Postgres. See **[DEPLOY.md](DEPLOY.md)** for both walkthroughs —
including TLS, which kiosk enrollment requires — and why serverless hosts
cannot run this.

The schema migrates itself at startup — there is no separate migration step.

## Adding a kiosk

In the admin UI, **Kiosks → Add kiosk** gives you a one-time code. On the
Raspberry Pi:

```bash
curl -sSL https://your-server/install.sh | sudo bash
```

It asks for the server URL and that code, then installs the dependencies, finds
the printer, enrolls for a key belonging to that device, writes both systemd
units and the browser autostart, starts everything and verifies it works.

Each kiosk gets its own credential, so one can be revoked without touching the
others, and no shared secret is ever copied between machines.

---

## Layout

```
app/
  routes/      auth, user, admin, api, agent (kiosk API), install
  services/    the actual logic — pricing, offers, stock, retention,
               enrollment, queue, printing, previews, receipts, ads
  templates/   server-rendered pages, including the kiosk display
  models.py    15 tables
print_agent/
  print_agent.py    the printing half — polls, prints, reports, heartbeats
  kiosk_server.py   the screen half — serves the display, generates the QR
  install.sh        one-command kiosk setup
scripts/       admin creation, schema upgrade, setup helpers
config.py      every knob, all overridable by environment variable
```

## Configuration

Everything has a working default. The ones worth knowing:

| Variable | Default | What it does |
|---|---|---|
| `DATABASE_URL` | SQLite file | Postgres in production |
| `DATA_DIR` | repo dir | Where uploads live — point at a mounted volume |
| `SITE_URL` | — | What the kiosk QR codes encode |
| `CLOUD_MODE` | `false` | Route printing through the agent instead of local CUPS |
| `AGENT_API_KEY` | — | Legacy shared kiosk key; leave blank and use enrollment |
| `PAPER_SIZES` | `A4` | Sizes offered to customers |
| `COLOR_PRINTING_ENABLED` | `false` | Show colour options |
| `FILE_RETENTION_MINUTES` | `0` | Grace period before a printed file is deleted |
| `PURGE_AFTER_PRINT` | `true` | Delete documents once the print is confirmed |

---

## On privacy and security

**Documents do not linger.** The server holds a file only between upload and the
moment the Pi confirms it printed, then deletes it along with its previews. The
job row survives for history and billing; the bytes do not. Failed jobs keep
their file for 24h so a print can be retried; abandoned uploads are cleared
after 72h. The Pi deletes its own copy as soon as CUPS is finished with it.

**Kiosks hold their own credentials.** Enrollment issues a per-device key,
stored server-side only as a SHA-256 hash and compared in constant time. A
kiosk is identified by its key rather than by a header it could put anything in.
Enrollment refuses to run over plain HTTP.

Also in place: bcrypt password hashing with account lockout, CSRF protection,
rate limiting, magic-byte validation and size caps on uploads, path-traversal
guards on every file-serving route, and sandboxed iframes for custom HTML
slides so a bad snippet cannot reach the kiosk's QR token.

`uploads/` and `instance/` are gitignored and must stay that way — they hold
real customer documents and the account database.

---

## Credits

Built by **Om Shukla** and **Avinav Gupta**, under the guidance of
**Mr. Girish Babu**, with thanks to **Prof. Mahalaxmi** for the printer.
