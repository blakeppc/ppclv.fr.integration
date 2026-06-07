"""
Daily BigQuery sync — pulls records changed in the last N days from FieldRoutes.

Runs every night via GitHub Actions. Keeps BigQuery current without
re-pulling 5 years of history each time.

What it updates:
  - fact_appointments  → any appointment updated in the lookback window
  - fact_tickets       → any ticket updated in the lookback window
  - fact_ticket_items  → line items for those tickets
  - dim_customers      → customers updated recently (active status, etc.)
  - dim_subscriptions  → subscriptions updated recently (price, frequency, etc.)

Dimension tables refreshed weekly (service types, employees, products):
  - These rarely change; a full overwrite runs on Mondays automatically.

Usage:
  python3 scripts/bq_daily_sync.py              # look back 2 days (default)
  python3 scripts/bq_daily_sync.py --days 7     # look back 7 days
  python3 scripts/bq_daily_sync.py --full-dims  # force refresh all dim tables
"""
import os
import sys
import time
import argparse
from datetime import datetime, timezone, timedelta

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.fieldroutes_client import client_from_env as fr_from_env
from scripts.bigquery_client import client_from_env as bq_from_env

BATCH_SIZE = 500
SLEEP_BETWEEN_CALLS = 1.1   # seconds — stay under 60 API calls/min


# ── Helpers ───────────────────────────────────────────────────────────────────

def lookback_start(days):
    """ISO timestamp for N days ago (UTC)."""
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return dt.strftime("%Y-%m-%d %H:%M:%S")

def fetch_ids_since(fr, search_fn, since_str, filter_params, id_key):
    """Search with dateUpdatedStart filter, return IDs."""
    params = {"dateUpdatedStart": since_str}
    params.update(filter_params)
    result = search_fn(params)
    time.sleep(SLEEP_BETWEEN_CALLS)
    return result.get(id_key, [])

def fetch_in_batches(get_fn, all_ids):
    """Fetch records in BATCH_SIZE chunks."""
    records = []
    for i in range(0, len(all_ids), BATCH_SIZE):
        batch = all_ids[i:i + BATCH_SIZE]
        recs = get_fn(batch)
        records.extend(recs)
        time.sleep(SLEEP_BETWEEN_CALLS)
    return records

def bq_delete_by_ids(bq, table, id_col, ids):
    """Delete rows from BQ table where id_col is in the given list."""
    if not ids:
        return
    # BQ IN clause has a limit but 500 IDs is fine
    id_list = ",".join(str(i) for i in ids)
    bq.query(f"""
        DELETE FROM `{bq.table_ref(table)}`
        WHERE {id_col} IN ({id_list})
    """)


# ── Fact table sync ───────────────────────────────────────────────────────────

def sync_appointments(fr, bq, office_num, since_str):
    """Sync appointments updated since since_str. Returns row count."""
    appt_ids = fetch_ids_since(fr, fr.search_appointments, since_str, {}, "appointmentIDs")
    if not appt_ids:
        return 0

    appointments = fetch_in_batches(fr.get_appointments, appt_ids)

    rows = [bq.appointment_row(a) for a in appointments
            if str(a.get("officeID", "-1")) != "-1"]

    if rows:
        # Delete old versions, then insert fresh
        bq_delete_by_ids(bq, "fact_appointments", "appointment_id", appt_ids)
        bq.load_rows("fact_appointments", rows)

    return len(rows)


def sync_tickets(fr, bq, office_num, since_str):
    """Sync tickets for appointments updated since since_str.

    NOTE: The FieldRoutes ticket search API ignores all date filters and always
    returns the same first 50k tickets regardless of params. We instead find
    recently-updated appointments, collect their ticket_ids, and fetch those
    tickets directly by ID — this is the only reliable way to get current data.
    """
    # Find appointments updated recently
    appt_ids = fetch_ids_since(fr, fr.search_appointments, since_str, {}, "appointmentIDs")
    if not appt_ids:
        return 0, 0

    # Get those appointments to extract their ticket_ids
    appointments = fetch_in_batches(fr.get_appointments, appt_ids)
    ticket_ids = list({
        int(a["ticketID"]) for a in appointments
        if a.get("ticketID") and int(a.get("ticketID", 0)) > 0
    })

    if not ticket_ids:
        return 0, 0

    tickets = fetch_in_batches(fr.get_tickets, ticket_ids)

    ticket_rows = []
    item_rows = []
    for t in tickets:
        if str(t.get("officeID", "-1")) != "-1":
            ticket_rows.append(bq.ticket_row(t))
            item_rows.extend(bq.ticket_item_rows(t))

    if ticket_rows:
        bq_delete_by_ids(bq, "fact_tickets", "ticket_id", ticket_ids)
        bq.load_rows("fact_tickets", ticket_rows)
    if item_rows:
        # Delete items belonging to these tickets
        id_list = ",".join(str(t["ticket_id"]) for t in ticket_rows)
        if id_list:
            bq.query(f"""
                DELETE FROM `{bq.table_ref("fact_ticket_items")}`
                WHERE ticket_id IN ({id_list})
            """)
        bq.load_rows("fact_ticket_items", item_rows)

    return len(ticket_rows), len(item_rows)


# ── Dimension table sync (incremental) ───────────────────────────────────────

def sync_customers(fr, bq, office_num, since_str):
    """Update dim_customers for customers changed recently."""
    cust_ids = fetch_ids_since(fr, fr.search_customers, since_str, {}, "customerIDs")
    if not cust_ids:
        return 0

    customers = fetch_in_batches(fr.get_customers, cust_ids)
    rows = [bq.customer_row(c) for c in customers if fr.is_real_record(c)]

    if rows:
        bq_delete_by_ids(bq, "dim_customers", "customer_id", [r["customer_id"] for r in rows])
        bq.load_rows("dim_customers", rows)

    return len(rows)


def sync_subscriptions(fr, bq, office_num, since_str):
    """Update dim_subscriptions for subscriptions changed recently."""
    sub_ids = fetch_ids_since(fr, fr.search_subscriptions, since_str, {}, "subscriptionIDs")
    if not sub_ids:
        return 0

    subs = fetch_in_batches(fr.get_subscriptions, sub_ids)
    rows = [bq.subscription_row(s) for s in subs if fr.is_real_record(s)]

    if rows:
        bq_delete_by_ids(bq, "dim_subscriptions", "subscription_id", [r["subscription_id"] for r in rows])
        bq.load_rows("dim_subscriptions", rows)

    return len(rows)


# ── Full dim refresh (weekly) ─────────────────────────────────────────────────

def full_dim_refresh(fr_clients, bq):
    """Full overwrite of service types, employees, and products (rarely change)."""
    print("  Full refresh: service types, employees, products...")

    for dim, search_fn_name, get_fn_name, id_key, row_fn_name in [
        ("dim_service_types", "search_service_types", "get_service_types", "serviceTypeIDs", "service_type_row"),
        ("dim_employees",     "search_employees",     "get_employees",     "employeeIDs",    "employee_row"),
        ("dim_products",      "search_products",      "get_products",      "productIDs",     "product_row"),
    ]:
        rows = []
        for office_num, fr in fr_clients.items():
            search_fn = getattr(fr, search_fn_name)
            get_fn    = getattr(fr, get_fn_name)
            row_fn    = getattr(bq, row_fn_name)

            result = search_fn()
            time.sleep(SLEEP_BETWEEN_CALLS)
            ids = result.get(id_key, [])

            for i in range(0, len(ids), BATCH_SIZE):
                batch = ids[i:i + BATCH_SIZE]
                records = get_fn(batch)
                time.sleep(SLEEP_BETWEEN_CALLS)
                for rec in records:
                    rows.append(row_fn(rec))

        bq.overwrite_table(dim, rows)
        print(f"    {dim}: {len(rows)} rows")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days",       type=int, default=2, help="Look-back window in days (default: 2)")
    parser.add_argument("--full-dims",  action="store_true", help="Force full refresh of all dim tables")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    is_monday = now.weekday() == 0  # Run full dim refresh on Mondays automatically

    print("=" * 65)
    print("BigQuery Daily Sync — FieldRoutes → BigQuery")
    print(f"  Run time:    {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Look-back:   {args.days} days")
    print(f"  Full dims:   {'yes' if (args.full_dims or is_monday) else 'no (Mon only)'}")
    print("=" * 65)

    # ── Connect ────────────────────────────────────────────────────────────────
    bq = bq_from_env()
    fr_clients = {}
    for n in [1, 2]:
        try:
            fr_clients[n] = fr_from_env(n)
        except ValueError:
            pass

    if not fr_clients:
        print("ERROR: No FieldRoutes offices connected.")
        return

    since_str = lookback_start(args.days)
    print(f"\nLooking back to: {since_str}\n")

    # ── Weekly full dim refresh (Mondays or forced) ────────────────────────────
    if args.full_dims or is_monday:
        full_dim_refresh(fr_clients, bq)

    # ── Per-office incremental sync ────────────────────────────────────────────
    totals = {
        "appointments": 0, "tickets": 0, "ticket_items": 0,
        "customers": 0, "subscriptions": 0,
    }

    for office_num, fr in sorted(fr_clients.items()):
        label = "Office 1 (Residential)" if office_num == 1 else "Office 2 (Commercial)"
        print(f"── {label} {'─'*(45-len(label))}")

        # Appointments
        n = sync_appointments(fr, bq, office_num, since_str)
        totals["appointments"] += n
        print(f"  Appointments:   {n:,} updated")

        # Tickets + items
        t, i = sync_tickets(fr, bq, office_num, since_str)
        totals["tickets"] += t
        totals["ticket_items"] += i
        print(f"  Tickets:        {t:,} updated  ({i:,} line items)")

        # Dim: customers
        n = sync_customers(fr, bq, office_num, since_str)
        totals["customers"] += n
        print(f"  Customers:      {n:,} updated")

        # Dim: subscriptions
        n = sync_subscriptions(fr, bq, office_num, since_str)
        totals["subscriptions"] += n
        print(f"  Subscriptions:  {n:,} updated")

        print()

    # ── Summary ────────────────────────────────────────────────────────────────
    print("=" * 65)
    print("Sync complete.")
    print(f"  Appointments:   {totals['appointments']:,}")
    print(f"  Tickets:        {totals['tickets']:,}  ({totals['ticket_items']:,} line items)")
    print(f"  Customers:      {totals['customers']:,}")
    print(f"  Subscriptions:  {totals['subscriptions']:,}")


if __name__ == "__main__":
    main()
