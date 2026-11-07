# Deploy guide

BTC Signal needs **no login, no registration, no API key and no paid plan**. Every live data source it uses
by default is free and public (Binance, Bybit, Coinbase, Kraken, Deribit, Hyperliquid, mempool/Blockstream,
Blockchain.com, Coin Metrics community, DeFiLlama, alternative.me, CoinGecko, Yahoo). Optional keys (CoinGlass,
Whale Alert, CryptoQuant, Glassnode, Etherscan, Dune) only switch on extra engines.

> Running on your own computer needs no account at all. Putting it on the internet always needs *some*
> machine to run on (a VPS or cloud host), and that host will have its own sign-up. GitHub itself also needs
> an account to upload code.

---

## Option 1 — Your own computer (simplest, no account)

Install once: **Python 3.12+** (python.org) and **Node.js 20+** (nodejs.org).

**Windows:** unzip, then double-click **`start.bat`**.

**macOS / Linux:**

```bash
bash start.sh
```

The first run installs dependencies (a few minutes). Then open **http://localhost:3000**.
The first signal appears within about a minute.

- Admin page (`/admin`) works without a token on this computer. It is refused from other machines.
- Data is stored in `backend/btc_signal.db` (SQLite). Delete it to start fresh.
- Stop: close the two terminal windows (Windows) or press Ctrl+C (macOS/Linux).

---

## Option 2 — Docker (any PC or server with Docker)

```bash
cp .env.example .env
```

Edit `.env`: set `POSTGRES_PASSWORD`. Set `BTCS_ADMIN_TOKEN` only if you want to use `/admin` from another
machine. If the server is not your own PC, set `PUBLIC_API_URL=http://YOUR-SERVER-IP:8000` and
`BTCS_CORS_ORIGINS=http://YOUR-SERVER-IP:3000`.

```bash
docker compose up --build -d
```

Website on port 3000, API on port 8000. This runs TimescaleDB + Redis + API + worker + web, and needs about
2 GB RAM. For a 1 GB server use Option 3.

---

## Option 3 — Small Linux server / VPS (works on 1 GB RAM)

Uses SQLite and one Python process; the website is built on your PC to avoid running out of memory.

**On your PC** (inside the project folder), build the website with your server's address:

```bash
cd frontend && NEXT_PUBLIC_API_URL=http://YOUR-SERVER-IP:8000 npm run build
```

Copy `frontend/.next/standalone`, `frontend/.next/static` and `frontend/public` to the server, keeping this layout:

```
web/server.js            (from .next/standalone)
web/.next/static/        (from .next/static)
web/public/              (from public)
```

**On the server** (Ubuntu/Debian):

```bash
sudo apt update && sudo apt install -y python3-venv nodejs
```

```bash
cd ~/btc-signal/backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Add 1–2 GB swap as a safety net:

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
```

Run the API (set a token because you will reach /admin remotely):

```bash
cd ~/btc-signal/backend && BTCS_ADMIN_TOKEN=pick-a-long-random-string BTCS_CORS_ORIGINS=http://YOUR-SERVER-IP:3000 .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Run the website:

```bash
cd ~/web && PORT=3000 HOSTNAME=0.0.0.0 node server.js
```

To keep both running after logout, create systemd services (example for the API):

```ini
# /etc/systemd/system/btc-api.service
[Unit]
Description=BTC Signal API
After=network-online.target

[Service]
WorkingDirectory=/home/YOUR-USER/btc-signal/backend
Environment=BTCS_ADMIN_TOKEN=pick-a-long-random-string
Environment=BTCS_CORS_ORIGINS=http://YOUR-SERVER-IP:3000
ExecStart=/home/YOUR-USER/btc-signal/backend/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now btc-api
```

Do the same for the website with `ExecStart=/usr/bin/node /home/YOUR-USER/web/server.js` and
`Environment=PORT=3000` / `Environment=HOSTNAME=0.0.0.0`. Open ports 3000 and 8000 in the firewall, or put
nginx in front for a domain + HTTPS.

Memory tips: if RAM is tight, disable the `hyperliquid_smart_money` provider in Admin → Providers (it
downloads a ~40 MB file twice a day).

---

## Upload to GitHub

1. Sign in to github.com → **New repository** (e.g. `btc-signal`), leave it empty.
2. **Unzip `btc-signal.zip` first.** GitHub does not unpack zip files. Then either:
   - Web: in the new repo click **uploading an existing file**, drag the *contents* of the unzipped folder in, commit; or
   - Command line, from inside the unzipped folder:

```bash
git init && git add . && git commit -m "BTC Signal" && git branch -M main
```

```bash
git remote add origin https://github.com/YOUR-USER/btc-signal.git && git push -u origin main
```

The zip excludes `.venv`, `node_modules`, `.next`, databases and logs. They are recreated on first run,
and `.gitignore` keeps them out of the repo. Never commit a `.env` file containing keys or tokens.

---

## Checking it works

- http://localhost:8000/api/v1/system/health → `"status": "ok"` once the first signal exists
- http://localhost:8000/api/v1/btc/signal → the current decision
- http://localhost:8000/docs → interactive API documentation

Market intelligence only, not financial advice.
