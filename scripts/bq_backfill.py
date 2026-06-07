"""
Historical backfill — pulls 5 years of FieldRoutes data into BigQuery.

Run this locally (not on GitHub Actions — it takes too long).
It saves progress after each month so you can stop and resume anytime.

Usage:
  python3 scripts/bq_backfill.py             # starts/resumes backfill
  python3 scripts/bq_backfill.py --dims-only # refresh dimension tables only
  python3 scripts/bq_backfill.py --status    # show progress without running

Checkpoint file: logs/bq_backfill_checkpoint.json
  Contains which months have been successfully loaded per office.
  Delete this file to start over from scratch.

Estimated run time: 3-8 hours total (depends on data volume and API rate).
The script will stop naturally when it's done or when running low on API calls.
You can safely Ctrl+C — it picks up where it left off.
"""
import os
import sys
import json
import time
import signal
import argparse
from datetime import date, datetime, timedelta
from dateutil.relativedelta import relativedelta

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.fieldroutes_client import client_from_env as fr_from_env
from scripts.bigquery_client import client_from_env as bq_from_env

CHECKPOINT_FILE = "logs/bq_backfill_checkpoint.json"
BATCH_SIZE = 500          # IDs per GET request (FR API max)
API_CALL_LIMIT = 2700     # Stop per-office if we approach the 3,000/day limit
SLEEP_BETWEEN_CALLS = 1.1 # seconds between FR API calls (stay under 60/min)
YEARS_BACK = 5            # How many years of historical data to load

# Graceful shutdown on Ctrl+C
_shutdown = False
def _handle_sigint(sig, frame):
    global _shutdown
    print("\n\nCtrl+C detected — finishing current batch then saving checkpoint...")
    _shutdown = True
signal.signal(signal.SIGINT, _handle_sigint)


# ── Checkpoint helpers ────────────────────────────────────────────────────────

def load_checkpoint():
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE) as f:
            return json.load(f)
    return {
        "dims_loaded_at": None,
        "completed_months": [],   # ["2021-01-office1", "2021-01-office2", ...]
        "api_calls": {"office1": 0, "office2": 0},
    }

def save_checkpoint(cp):
    os.makedirs("logs", exist_ok=True)
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump(cp, f, indent=2)

def month_key(year, month, office_num):
    return f"{year:04d}-{month:02d}-office{office_num}"


# ── Month range helpers ───────────────────────────────────────────────────────

def months_to_process():
    """Returns list of (year, month) tuples covering YEARS_BACK years up to today."""
    today = date.today()
    # Start from YEARS_BACK ago, first day of that month
    start = date(today.year - YEARS_BACK, today.month, 1)
    months = []
    current = start
    while current <= today:
        months.append((current.year, current.month))
        current += relativedelta(months=1)
    return months

def month_date_range(year, month):
    """Returns (start_str, end_str) for a month, e.g. ('2024-03-01', '2024-03-31')."""
    start = date(year, month, 1)
    # Last day of month
    if month == 12:
        end = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        end = date(year, month + 1, 1) - timedelta(days=1)
    return start.isoformat(), end.isoformat()


# ── API fetch helpers ─────────────────────────────────────────────────────────

def fetch_all_ids(fr, search_fn, filter_params, id_key, call_counter):
    """Call search endpoint, return list of IDs. Increments call_counter[0]."""
    result = search_fn(filter_params)
    call_counter[0] += 1
    time.sleep(SLEEP_BETWEEN_CALLS)
    return result.get(id_key, [])

def fetch_in_batches(fr, get_fn, all_ids, call_counter):
    """Fetch records in BATCH_SIZE chunks. Returns list of all records."""
    records = []
    for i in range(0, len(all_ids), BATCH_SIZE):
        if _shutdown:
            break
        batch = all_ids[i:i + BATCH_SIZE]
        recs = get_fn(batch)
        call_counter[0] += 1
        records.extend(recs)
        time.sleep(SLEEP_BETWEEN_CALLS)
    return records


# ── Dimension table loaders ───────────────────────────────────────────────────

def load_dim_service_types(fr_clients, bq):
    """Pull all service types from both offices → overwrite dim_service_types."""
    print("  Loading service types...")
    rows = []
    for office_num, fr in fr_clients.items():
        result = fr.search_service_types()
        time.sleep(SLEEP_BETWEEN_CALLS)
        type_ids = result.get("serviceTypeIDs", [])
        print(f"    Office {office_num}: {len(type_ids)} service types")

        for i in range(0, len(type_ids), BATCH_SIZE):
            batch = type_ids[i:i + BATCH_SIZE]
            types = fr.get_service_types(batch)
            time.sleep(SLEEP_BETWEEN_CALLS)
            for st in types:
                rows.append(bq.service_type_row(st))

    bq.overwrite_table("dim_service_types", rows)
    print(f"    → {len(rows)} rows written to dim_service_types")
    return 2  # approx API calls (1 search + 1 get per office, simplified)


def load_dim_employees(fr_clients, bq):
    """Pull all employees from both offices → overwrite dim_employees."""
    print("  Loading employees...")
    rows = []
    for office_num, fr in fr_clients.items():
        result = fr.search_employees()
        time.sleep(SLEEP_BETWEEN_CALLS)
        emp_ids = result.get("employeeIDs", [])
        print(f"    Office {office_num}: {len(emp_ids)} employees")

        for i in range(0, len(emp_ids), BATCH_SIZE):
            batch = emp_ids[i:i + BATCH_SIZE]
            emps = fr.get_employees(batch)
            time.sleep(SLEEP_BETWEEN_CALLS)
            for emp in emps:
                rows.append(bq.employee_row(emp))

    bq.overwrite_table("dim_employees", rows)
    print(f"    → {len(rows)} rows written to dim_employees")
    return 4


def load_dim_products(fr_clients, bq):
    """Pull all products (chemicals/materials) from both offices → overwrite dim_products."""
    print("  Loading products...")
    rows = []
    for office_num, fr in fr_clients.items():
        result = fr.search_products()
        time.sleep(SLEEP_BETWEEN_CALLS)
        product_ids = result.get("productIDs", [])
        print(f"    Office {office_num}: {len(product_ids)} products")

        for i in range(0, len(product_ids), BATCH_SIZE):
            batch = product_ids[i:i + BATCH_SIZE]
            products = fr.get_products(batch)
            time.sleep(SLEEP_BETWEEN_CALLS)
            for p in products:
                rows.append(bq.product_row(p))

    bq.overwrite_table("dim_products", rows)
    print(f"    → {len(rows)} rows written to dim_products")
    return 4


def load_dim_customers(fr_clients, bq):
    """Pull ALL customers (active + inactive) from both offices → overwrite dim_customers."""
    print("  Loading customers (all — active + cancelled)...")
    rows = []
    total_calls = 0

    for office_num, fr in fr_clients.items():
        # No filter = all customers
        result = fr.search_customers()
        time.sleep(SLEEP_BETWEEN_CALLS)
        total_calls += 1
        cust_ids = result.get("customerIDs", [])
        print(f"    Office {office_num}: {len(cust_ids):,} customers ({len(cust_ids) // BATCH_SIZE + 1} batches)")

        for i in range(0, len(cust_ids), BATCH_SIZE):
            if _shutdown:
                break
            batch = cust_ids[i:i + BATCH_SIZE]
            customers = fr.get_customers(batch)
            total_calls += 1
            time.sleep(SLEEP_BETWEEN_CALLS)
            for c in customers:
                if fr.is_real_record(c):
                    rows.append(bq.customer_row(c))

            if i % 5000 == 0 and i > 0:
                print(f"      {i:,}/{len(cust_ids):,} fetched...")

    bq.overwrite_table("dim_customers", rows)
    print(f"    → {len(rows):,} rows written to dim_customers")
    return total_calls


def load_dim_subscriptions(fr_clients, bq):
    """Pull ALL subscriptions (active + cancelled) → overwrite dim_subscriptions."""
    print("  Loading subscriptions (all)...")
    rows = []
    total_calls = 0

    for office_num, fr in fr_clients.items():
        result = fr.search_subscriptions()
        time.sleep(SLEEP_BETWEEN_CALLS)
        total_calls += 1
        sub_ids = result.get("subscriptionIDs", [])
        print(f"    Office {office_num}: {len(sub_ids):,} subscriptions ({len(sub_ids) // BATCH_SIZE + 1} batches)")

        for i in range(0, len(sub_ids), BATCH_SIZE):
            if _shutdown:
                break
            batch = sub_ids[i:i + BATCH_SIZE]
            subs = fr.get_subscriptions(batch)
            total_calls += 1
            time.sleep(SLEEP_BETWEEN_CALLS)
            for s in subs:
                if fr.is_real_record(s):
                    rows.append(bq.subscription_row(s))

            if i % 5000 == 0 and i > 0:
                print(f"      {i:,}/{len(sub_ids):,} fetched...")

    bq.overwrite_table("dim_subscriptions", rows)
    print(f"    → {len(rows):,} rows written to dim_subscriptions")
    return total_calls


def load_dims(fr_clients, bq, cp):
    """Load all dimension tables. Returns total API calls used."""
    print("\n── Dimension Tables ────────────────────────────────────────")
    total = 0
    total += load_dim_service_types(fr_clients, bq)
    total += load_dim_employees(fr_clients, bq)
    total += load_dim_products(fr_clients, bq)
    total += load_dim_customers(fr_clients, bq)
    total += load_dim_subscriptions(fr_clients, bq)
    cp["dims_loaded_at"] = datetime.utcnow().isoformat()
    save_checkpoint(cp)
    print(f"\n  Dimension tables complete. API calls used: ~{total}")
    return total


# ── Fact table loaders (monthly) ──────────────────────────────────────────────

def delete_month_from_bq(bq, table, date_col, start_str, end_str):
    """Delete existing rows for a date range before re-inserting (idempotent load)."""
    try:
        bq.query(f"""
            DELETE FROM `{bq.table_ref(table)}`
            WHERE {date_col} >= '{start_str}' AND {date_col} <= '{end_str}'
        """)
    except Exception as e:
        # Table might be empty or date column NULL — not fatal
        pass


def load_month_appointments(fr, bq, office_num, year, month, call_counter):
    """Load one month of appointments for one office. Returns row count."""
    start_str, end_str = month_date_range(year, month)

    # Search for appointment IDs in this date range
    appt_ids = fetch_all_ids(
        fr,
        fr.search_appointments,
        {"dateStart": start_str, "dateEnd": end_str},
        "appointmentIDs",
        call_counter,
    )

    if not appt_ids:
        return 0

    # Delete any existing data for this month/office (makes load idempotent)
    delete_month_from_bq(bq, "fact_appointments", "scheduled_date", start_str, end_str)

    # Fetch and transform records
    rows = []
    for i in range(0, len(appt_ids), BATCH_SIZE):
        if _shutdown:
            break
        batch = appt_ids[i:i + BATCH_SIZE]
        appts = fr.get_appointments(batch)
        call_counter[0] += 1
        time.sleep(SLEEP_BETWEEN_CALLS)
        for a in appts:
            if str(a.get("officeID", "-1")) != "-1":
                rows.append(bq.appointment_row(a))

    if rows:
        bq.load_rows("fact_appointments", rows)

    return len(rows)


def load_month_tickets(fr, bq, office_num, year, month, call_counter):
    """Load tickets for appointments scheduled in this month.

    NOTE: The FR ticket search API ignores all date filters and always returns
    the same first 50k tickets regardless of date params. Using it in the
    backfill consumed ~100 API calls per month (50k IDs / 500 per batch),
    exhausting the 3,000/day limit and causing later months to silently load
    0 appointments. The correct approach: collect ticket_ids from the
    appointments we just loaded for this month, then fetch those tickets by ID.
    """
    start_str, end_str = month_date_range(year, month)

    # Get ticket IDs from appointments already loaded for this month/office
    rows = bq.query(f"""
        SELECT DISTINCT ticket_id
        FROM `{bq.table_ref("fact_appointments")}`
        WHERE office_id = {office_num}
          AND scheduled_date >= '{start_str}'
          AND scheduled_date <= '{end_str}'
          AND ticket_id IS NOT NULL
          AND ticket_id > 0
    """)
    ticket_ids = [r.ticket_id for r in rows]

    if not ticket_ids:
        return 0, 0

    # Delete existing rows for these specific ticket IDs (idempotent)
    id_list = ",".join(str(i) for i in ticket_ids)
    try:
        bq.query(f"DELETE FROM `{bq.table_ref('fact_tickets')}` WHERE ticket_id IN ({id_list})")
        bq.query(f"DELETE FROM `{bq.table_ref('fact_ticket_items')}` WHERE ticket_id IN ({id_list})")
    except Exception:
        pass  # table may be empty on first run

    ticket_rows = []
    item_rows = []

    for i in range(0, len(ticket_ids), BATCH_SIZE):
        if _shutdown:
            break
        batch = ticket_ids[i:i + BATCH_SIZE]
        tickets = fr.get_tickets(batch)
        call_counter[0] += 1
        time.sleep(SLEEP_BETWEEN_CALLS)
        for t in tickets:
            if str(t.get("officeID", "-1")) != "-1":
                ticket_rows.append(bq.ticket_row(t))
                item_rows.extend(bq.ticket_item_rows(t))

    if ticket_rows:
        bq.load_rows("fact_tickets", ticket_rows)
    if item_rows:
        bq.load_rows("fact_ticket_items", item_rows)

    return len(ticket_rows), len(item_rows)


# ── Main ──────────────────────────────────────────────────────────────────────

def print_status(cp):
    """Print current backfill progress."""
    completed = set(cp.get("completed_months", []))
    all_months = months_to_process()
    total_slots = len(all_months) * 2  # 2 offices

    done = sum(
        1 for (y, m) in all_months
        for o in [1, 2]
        if month_key(y, m, o) in completed
    )

    print(f"\nBackfill status:")
    print(f"  Date range:  {all_months[0][0]}-{all_months[0][1]:02d} → {all_months[-1][0]}-{all_months[-1][1]:02d}")
    print(f"  Progress:    {done}/{total_slots} office-months complete ({done/total_slots*100:.0f}%)")
    print(f"  Dims loaded: {cp.get('dims_loaded_at', 'not yet')}")
    print(f"  Checkpoint:  {CHECKPOINT_FILE}")
    if done < total_slots:
        remaining = total_slots - done
        print(f"  Remaining:   {remaining} office-months (~{remaining * 2} min estimated)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dims-only", action="store_true", help="Refresh dimension tables only, skip fact tables")
    parser.add_argument("--status",    action="store_true", help="Show progress and exit")
    parser.add_argument("--start-year", type=int, default=None, help="Override start year (default: 5 years ago)")
    args = parser.parse_args()

    global YEARS_BACK
    if args.start_year:
        YEARS_BACK = date.today().year - args.start_year

    # ── Load checkpoint ────────────────────────────────────────────────────────
    cp = load_checkpoint()

    if args.status:
        print_status(cp)
        return

    print("=" * 65)
    print("BigQuery Backfill — FieldRoutes Historical Data")
    print(f"Date range: {date.today().year - YEARS_BACK} → {date.today().year}")
    print("=" * 65)
    print("\nPress Ctrl+C at any time to pause — progress is saved automatically.")

    # ── Connect ────────────────────────────────────────────────────────────────
    print("\nConnecting...")
    bq = bq_from_env()
    print(f"  BigQuery: {bq.project}.{bq.dataset}")

    fr_clients = {}
    for n in [1, 2]:
        try:
            fr_clients[n] = fr_from_env(n)
            print(f"  Office {n}: connected")
        except ValueError as e:
            print(f"  Office {n}: SKIPPED — {e}")

    if not fr_clients:
        print("ERROR: No FieldRoutes offices connected.")
        return

    # ── Dimension tables ───────────────────────────────────────────────────────
    dims_done = cp.get("dims_loaded_at") is not None
    if not dims_done or args.dims_only:
        if dims_done:
            print(f"\nDimension tables were loaded on {cp['dims_loaded_at'][:10]}. Refreshing...")
        load_dims(fr_clients, bq, cp)
        save_checkpoint(cp)

    if args.dims_only:
        print("\nDimension tables refreshed. Done.")
        return

    if _shutdown:
        print("Stopped after dimension tables.")
        return

    # ── Fact tables — month by month ───────────────────────────────────────────
    all_months = months_to_process()
    completed = set(cp.get("completed_months", []))

    print(f"\n── Fact Tables ─────────────────────────────────────────────")
    print(f"   {len(all_months)} months × 2 offices = {len(all_months)*2} total office-months")
    print(f"   Already completed: {len(completed)}")
    print()

    total_appt_rows = 0
    total_ticket_rows = 0
    total_item_rows = 0
    months_this_run = 0

    for year, month in all_months:
        if _shutdown:
            break

        for office_num, fr in sorted(fr_clients.items()):
            if _shutdown:
                break

            key = month_key(year, month, office_num)
            if key in completed:
                continue

            print(f"  {year}-{month:02d} Office {office_num}:", end="", flush=True)

            call_counter = [0]

            try:
                # Appointments
                appt_count = load_month_appointments(fr, bq, office_num, year, month, call_counter)
                total_appt_rows += appt_count

                # Tickets + items
                ticket_count, item_count = load_month_tickets(fr, bq, office_num, year, month, call_counter)
                total_ticket_rows += ticket_count
                total_item_rows += item_count

                print(f" {appt_count:,} appts | {ticket_count:,} tickets | {item_count:,} items"
                      f" ({call_counter[0]} API calls)")

                if not _shutdown:
                    completed.add(key)
                    cp["completed_months"] = list(completed)
                    save_checkpoint(cp)
                    months_this_run += 1

            except Exception as e:
                print(f" ERROR: {e}")
                # Don't mark as complete — will retry on next run
                save_checkpoint(cp)

    # ── Summary ────────────────────────────────────────────────────────────────
    remaining = (len(all_months) * len(fr_clients)) - len(completed)

    print(f"\n{'=' * 65}")
    if _shutdown:
        print("Paused (Ctrl+C) — progress saved.")
    elif remaining == 0:
        print("Backfill COMPLETE!")
    else:
        print(f"Run complete — {remaining} office-months remaining.")

    print(f"  Months processed this run: {months_this_run}")
    print(f"  Appointments loaded:       {total_appt_rows:,}")
    print(f"  Tickets loaded:            {total_ticket_rows:,}")
    print(f"  Ticket items loaded:       {total_item_rows:,}")
    print(f"  Progress saved to:         {CHECKPOINT_FILE}")

    if remaining > 0 and not _shutdown:
        print(f"\n  Run again tomorrow to continue (API limit resets daily).")
        print(f"  Or run again now — the script will pick up where it left off.")


if __name__ == "__main__":
    main()
