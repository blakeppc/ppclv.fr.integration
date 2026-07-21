"""
Daily BigQuery sync — pulls records changed in the last N days from FieldRoutes.

Runs every night via GitHub Actions. Keeps BigQuery current without
re-pulling 5 years of history each time.

What it updates:
  - fact_appointments  → any appointment scheduled in the lookback window
  - fact_tickets       → tickets for those appointments (fetched by ID)
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

def build_route_tech_map(fr, office_num, date_start, date_end):
    """Map {route_id: assigned_tech_id} for every route in the date window.

    A pending appointment's own assignedTech field is frequently 0/unset —
    FieldRoutes carries the real assignment on the route, not the appointment,
    until the job is completed. Fetching routes per day lets us attribute
    pending appointments to the correct tech. route/search takes a single
    'date' (no range), so we walk each calendar day in the window.
    """
    route_tech = {}
    d = datetime.strptime(date_start, "%Y-%m-%d").date()
    end = datetime.strptime(date_end, "%Y-%m-%d").date()
    while d <= end:
        try:
            result = fr.search_routes(d.strftime("%Y-%m-%d"), office_num)
            time.sleep(SLEEP_BETWEEN_CALLS)
            route_ids = result.get("routeIDs", [])
            for i in range(0, len(route_ids), BATCH_SIZE):
                batch = route_ids[i:i + BATCH_SIZE]
                routes = fr.get_routes(batch)
                time.sleep(SLEEP_BETWEEN_CALLS)
                for r in routes:
                    rid = _to_int(r.get("routeID"))
                    tech = _to_int(r.get("assignedTech"))
                    if rid is not None and tech:  # skip 0/None — no real tech assigned
                        route_tech[rid] = tech
        except Exception as e:
            print(f"    WARNING: route fetch for {d} (office {office_num}) failed — {e}")
        d += timedelta(days=1)
    return route_tech


def _to_int(val):
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def sync_appointments(fr, bq, office_num, since_str):
    """Sync appointments scheduled within the lookback window. Returns row count.

    Uses dateStart/dateEnd (scheduled date) rather than dateAddedStart.
    This correctly captures commercial appointments scheduled months in advance
    — their tickets are created when the appointment is booked, so using
    dateAddedStart would miss them entirely once they're older than the lookback.

    dateStart/dateEnd confirmed working on the FR API (unlike dateUpdatedStart,
    scheduledStart, dateCompletedStart, and status filters which all return
    the same 50,000 oldest records regardless of value).
    """
    date_start = since_str[:10]
    date_end   = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # FR's dateStart/dateEnd search only returns active appointments — it silently
    # drops cancelled (status=-1) and rescheduled (status=-2) ones. We track which
    # IDs were already in BQ for this window so we can re-fetch their current status
    # and preserve them (needed for same-day cancel/reschedule reporting).
    existing_rows = bq.query(f"""
        SELECT DISTINCT appointment_id
        FROM `{bq.table_ref("fact_appointments")}`
        WHERE office_id = {office_num}
          AND scheduled_date BETWEEN '{date_start}' AND '{date_end}'
    """)
    existing_ids = {r.appointment_id for r in existing_rows}

    result = fr.search_appointments({"dateStart": date_start, "dateEnd": date_end})
    time.sleep(SLEEP_BETWEEN_CALLS)
    active_ids = set(result.get("appointmentIDs", []))
    if not active_ids and not existing_ids:
        return 0

    appointments = fetch_in_batches(fr.get_appointments, list(active_ids)) if active_ids else []

    # Re-fetch any IDs that were in BQ but dropped from FR search — these are
    # cancelled/rescheduled. FR returns them with their original scheduled_date
    # and actual status (-1 Cancelled / -2 Re-Scheduled).
    inactive_ids = existing_ids - active_ids
    if inactive_ids:
        inactive_appts = fetch_in_batches(fr.get_appointments, list(inactive_ids))
        appointments.extend(inactive_appts)

    # Route-level tech assignment (reliable source for pending appointments whose
    # own assignedTech field is still unset).
    route_tech_map = build_route_tech_map(fr, office_num, date_start, date_end)

    rows = [bq.appointment_row(a, route_tech_map) for a in appointments
            if str(a.get("officeID", "-1")) != "-1"]

    if rows:
        bq.query(f"""
            DELETE FROM `{bq.table_ref("fact_appointments")}`
            WHERE office_id = {office_num}
              AND scheduled_date BETWEEN '{date_start}' AND '{date_end}'
        """)
        bq.load_rows("fact_appointments", rows)

    return len(rows)


def sync_tickets(fr, bq, office_num, since_str):
    """Sync tickets for appointments scheduled in the lookback window.

    NOTE: FR ticket/search ignores ALL date filters (always returns same 50k).
    So we query BigQuery for ticket_ids from appointments scheduled in the
    lookback window — sync_appointments already loaded those — then fetch
    the tickets directly by ID from the FR API.
    """
    date_start = since_str[:10]
    date_end   = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = bq.query(f"""
        SELECT DISTINCT ticket_id
        FROM `{bq.table_ref("fact_appointments")}`
        WHERE office_id = {office_num}
          AND scheduled_date BETWEEN '{date_start}' AND '{date_end}'
          AND ticket_id IS NOT NULL AND ticket_id > 0
    """)
    ticket_ids = [r.ticket_id for r in rows]

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

    failed_offices = []

    for office_num, fr in sorted(fr_clients.items()):
        label = "Office 1 (Residential)" if office_num == 1 else "Office 2 (Commercial)"
        office_name = "Residential" if office_num == 1 else "Commercial"
        print(f"── {label} {'─'*(45-len(label))}")

        counts = {"appointments": 0, "tickets": 0, "ticket_items": 0,
                  "customers": 0, "subscriptions": 0}
        error_msg = None

        try:
            counts["appointments"] = sync_appointments(fr, bq, office_num, since_str)
            print(f"  Appointments:   {counts['appointments']:,} updated")

            counts["tickets"], counts["ticket_items"] = sync_tickets(fr, bq, office_num, since_str)
            print(f"  Tickets:        {counts['tickets']:,} updated  ({counts['ticket_items']:,} line items)")

            counts["customers"] = sync_customers(fr, bq, office_num, since_str)
            print(f"  Customers:      {counts['customers']:,} updated")

            counts["subscriptions"] = sync_subscriptions(fr, bq, office_num, since_str)
            print(f"  Subscriptions:  {counts['subscriptions']:,} updated")

        except Exception as e:
            error_msg = str(e)
            print(f"  ERROR: Office {office_num} sync failed — {e}")
            print(f"  Skipping Office {office_num}; other offices unaffected.")
            failed_offices.append(office_num)

        # Write health record regardless of success or failure
        try:
            bq.load_rows("sync_health", [{
                "run_at":               now.isoformat(),
                "office_id":            office_num,
                "office_name":          office_name,
                "status":               "error" if error_msg else "success",
                "error_message":        error_msg,
                "lookback_days":        args.days,
                "appointments_synced":  counts["appointments"],
                "tickets_synced":       counts["tickets"],
                "ticket_items_synced":  counts["ticket_items"],
                "customers_synced":     counts["customers"],
                "subscriptions_synced": counts["subscriptions"],
            }])
        except Exception as health_err:
            print(f"  WARNING: Could not write sync_health record — {health_err}")

        for k, v in counts.items():
            if k in ("ticket_items",):
                continue
            totals[k] += v
        totals["ticket_items"] += counts["ticket_items"]

        print()

    # ── Summary ────────────────────────────────────────────────────────────────
    print("=" * 65)
    if failed_offices:
        print(f"Sync finished WITH ERRORS — Office(s) {failed_offices} failed after retries.")
    else:
        print("Sync complete.")
    print(f"  Appointments:   {totals['appointments']:,}")
    print(f"  Tickets:        {totals['tickets']:,}  ({totals['ticket_items']:,} line items)")
    print(f"  Customers:      {totals['customers']:,}")
    print(f"  Subscriptions:  {totals['subscriptions']:,}")

    if failed_offices:
        sys.exit(1)


if __name__ == "__main__":
    main()
