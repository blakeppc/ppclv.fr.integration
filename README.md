# FieldRoutes Integration Suite
**Preventive Pest Control — Automated workflows for both offices**

Two independent integrations sharing the same codebase:
- **Office 1** — Preventive Las Vegas (`ppclv1.fieldroutes.com`) — ~57,000 customers
- **Office 2** — Preventive Pest Control Commercial (`ppclv2.fieldroutes.com`) — ~10,800 customers

---

## Quick Start

### 1. Install Python (one-time)
Open Terminal (Command + Space → type "Terminal"):
```bash
python3 --version
```
If you see `Python 3.x.x`, you're good. If not, download from python.org/downloads.

### 2. Install dependencies (one-time)
```bash
cd ~/fieldroutes-integrations
pip3 install -r requirements.txt
```

### 3. Set up your credentials
Copy `.env.example` to `.env`, then fill in your API keys for each office.
(See `.env.example` for the format — never share the `.env` file.)

### 4. Test the connection
```bash
python3 scripts/test_connection.py
```
Both offices should show "Connected" with their customer counts.

---

## Project Structure

```
fieldroutes-integrations/
├── scripts/
│   ├── fieldroutes_client.py      ← API connection (don't edit)
│   ├── test_connection.py         ← Run this to verify both offices work
│   ├── ghl_initial_match.py       ← (Phase 2) One-time FR→GHL contact matching
│   ├── ghl_sync_office1.py        ← (Phase 2) Daily sync for Office 1
│   ├── ghl_sync_office2.py        ← (Phase 2) Daily sync for Office 2
│   ├── price_increase_office1.py  ← (Phase 3) Price increase automation for Office 1
│   └── price_increase_office2.py  ← (Phase 3) Price increase automation for Office 2
├── config/
│   ├── office1_config.json        ← Office 1 settings (you edit this)
│   └── office2_config.json        ← Office 2 settings (you edit this)
├── logs/                          ← Run logs and price increase history (auto-created)
├── .env.example                   ← Credential template (safe to share/commit)
├── .env                           ← Your real credentials (NEVER commit or share)
├── .gitignore                     ← Prevents .env from being uploaded to GitHub
└── requirements.txt               ← Python packages
```

---

## Configuration

Each office has its own config file in the `config/` folder.
Edit the values to control behavior — all settings have explanations inside the file.

| Setting | What it does |
|---------|-------------|
| `increase.value` | How much to raise (5 = 5% or $5 depending on `increase.type`) |
| `eligibility.minimum_days_since_last_increase` | Only raise if no increase in this many days |
| `eligibility.minimum_recurring_charge` | Only raise subscriptions above this dollar amount |
| `safety.dry_run` | **Keep true until ready to go live — never makes real changes when true** |

---

## Deploying to Railway (So Scripts Run Automatically)

### Step 1 — Push to GitHub
```bash
# Run these commands in Terminal from the project folder
cd ~/fieldroutes-integrations
git init
git add .
git commit -m "Initial setup"
# Then follow GitHub's instructions to push to your private repo
```

### Step 2 — Connect Railway
1. Go to railway.app → Login with GitHub
2. New Project → Deploy from GitHub Repo → select `fieldroutes-integrations`
3. Create two services: `office1` and `office2`

### Step 3 — Add environment variables in Railway
For each service, add the variables from your `.env` file under **Variables**.
Only add the variables for that office's service.

### Step 4 — Set cron schedules
In each service's settings, under **Cron Schedule**:
- GHL sync: `0 2 * * *` (runs at 2am every day)
- Price increase: `0 9 1 * *` (runs at 9am on the 1st of each month)

---

## Running Scripts Manually

```bash
# Test both office connections
python3 scripts/test_connection.py

# Price increase — dry run (no real changes)
python3 scripts/price_increase_office1.py
python3 scripts/price_increase_office2.py

# GHL daily sync
python3 scripts/ghl_sync_office1.py
python3 scripts/ghl_sync_office2.py
```
