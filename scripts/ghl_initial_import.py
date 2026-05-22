"""
One-time script: imports all active FieldRoutes customers into GoHighLevel.
Run this locally once to populate your GHL account with FR customer data.

Dry run (default — safe, makes no changes to GHL):
  python3 scripts/ghl_initial_import.py

Live run (actually creates contacts in GHL):
  python3 scripts/ghl_initial_import.py --live

Resume an interrupted run (skips already-imported customers):
  python3 scripts/ghl_initial_import.py --live
  (the checkpoint file is saved automatically as you go)
"""
import os
import sys
import json
import time
import argparse
from datetime import datetime

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.fieldroutes_client import client_from_env as fr_from_env
from scripts.ghl_client import client_from_env as ghl_from_env, format_phone

CHECKPOINT_FILE = "logs/ghl_import_checkpoint.json"
LOG_FILE = "logs/ghl_import_log.json"

FR_CUSTOMER_ID_FIELD_KEY = "contact.fr_customer_id"
FR_CUSTOMER_ID_FIELD_NAME = "FR Customer ID"

OFFICES = {1: "fr_office1", 2: "fr_office2"}


def build_ghl_payload(customer, office_num, fr_id_field_id):
    """Convert a FieldRoutes customer record to a GHL contact payload."""
    fr_id = str(customer.get("customerID", ""))
    is_commercial = str(customer.get("commercialAccount", "0")) == "1"

    tags = [
        OFFICES[office_num],
        "fr_active",
        "fr_commercial" if is_commercial else "fr_residential",
    ]

    phone = format_phone(customer.get("phone1")) or format_phone(customer.get("phone2"))
    email = (customer.get("email") or "").strip().lower() or None

    payload = {
        "firstName": (customer.get("fname") or "").strip(),
        "lastName": (customer.get("lname") or "").strip(),
        "tags": tags,
        "customFields": [{"id": fr_id_field_id, "value": fr_id}],
    }

    if email:
        payload["email"] = email
    if phone:
        payload["phone"] = phone

    company = (customer.get("companyName") or "").strip()
    if company:
        payload["companyName"] = company

    for fr_field, ghl_field in [
        ("address", "address1"),
        ("city", "city"),
        ("state", "state"),
        ("zip", "postalCode"),
    ]:
        val = (customer.get(fr_field) or "").strip()
        if val:
            payload[ghl_field] = val

    return payload


def load_checkpoint():
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE) as f:
            return json.load(f)
    return {"imported_fr_ids": []}


def save_checkpoint(data):
    os.makedirs("logs", exist_ok=True)
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump(data, f)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--live",
        action="store_true",
        help="Actually create contacts in GHL (default is dry run)",
    )
    args = parser.parse_args()
    dry_run = not args.live

    print("=" * 65)
    print("GHL Initial Import — FieldRoutes → GoHighLevel")
    print(f"Mode: {'DRY RUN (no changes will be made)' if dry_run else '*** LIVE — creating contacts in GHL ***'}")
    print("=" * 65)

    print("\nConnecting to GoHighLevel...")
    ghl = ghl_from_env()
    print(f"  Connected to location: {ghl.location_id}")

    if not dry_run:
        print("  Ensuring 'FR Customer ID' custom field exists in GHL...")
        fr_id_field_id = ghl.ensure_custom_field(FR_CUSTOMER_ID_FIELD_NAME)
        print(f"  Custom field ready (ID: {fr_id_field_id})")
    else:
        fr_id_field_id = "DRY_RUN_FIELD_ID"

    checkpoint = load_checkpoint()
    already_imported = set(str(x) for x in checkpoint.get("imported_fr_ids", []))
    if already_imported:
        print(f"  Resuming: {len(already_imported):,} customers already imported from a previous run")

    total_stats = {"created": 0, "updated": 0, "skipped_no_contact": 0, "errors": 0}
    log_entries = []

    for office_num in [1, 2]:
        print(f"\n{'─' * 65}")
        print(f"Office {office_num} ({'Las Vegas Residential' if office_num == 1 else 'Commercial'})")
        print(f"{'─' * 65}")

        try:
            fr = fr_from_env(office_num)
        except ValueError as e:
            print(f"  SKIPPED — {e}")
            continue

        print("  Fetching active customer IDs from FieldRoutes...")
        result = fr.search_customers({"active": 1})
        all_ids = result.get("customerIDs", [])
        print(f"  {len(all_ids):,} active customer IDs returned")

        to_import = [str(cid) for cid in all_ids if str(cid) not in already_imported]
        skipped_count = len(all_ids) - len(to_import)
        if skipped_count:
            print(f"  Skipping {skipped_count:,} already imported — {len(to_import):,} remaining")

        print(f"  Fetching customer records and importing to GHL...")
        batch_size = 100
        processed = 0

        for i in range(0, len(to_import), batch_size):
            batch_ids = to_import[i : i + batch_size]
            customers = None
            for attempt in range(3):
                try:
                    customers = fr.get_customers(batch_ids)
                    break
                except Exception as e:
                    if attempt < 2:
                        wait = 65 if "maximum number of read requests" in str(e) else 5
                        print(f"  Rate limit hit — waiting {wait}s before retry...")
                        time.sleep(wait)
                    else:
                        print(f"  ERROR fetching batch {i}-{i+batch_size} after 3 attempts: {e}")
                        total_stats["errors"] += len(batch_ids)
            if customers is None:
                continue

            for customer in customers:
                fr_id = str(customer.get("customerID", ""))

                if not fr.is_real_record(customer):
                    continue
                if fr_id in already_imported:
                    continue

                payload = build_ghl_payload(customer, office_num, fr_id_field_id)

                if not payload.get("email") and not payload.get("phone"):
                    total_stats["skipped_no_contact"] += 1
                    log_entries.append({"fr_id": fr_id, "action": "skipped", "reason": "no email or phone"})
                    continue

                if dry_run:
                    total_stats["created"] += 1
                    log_entries.append({
                        "fr_id": fr_id,
                        "action": "would_create",
                        "name": f"{payload.get('firstName')} {payload.get('lastName')}",
                        "email": payload.get("email"),
                    })
                else:
                    try:
                        # Initial import: GHL is empty so create directly (no search needed).
                        # If GHL rejects due to a duplicate, fall back to upsert.
                        try:
                            contact = ghl.create_contact(payload)
                            action = "created"
                        except Exception as create_err:
                            err_text = str(create_err)
                            if "duplicate" in err_text.lower() or "already exist" in err_text.lower() or "422" in err_text or "400" in err_text:
                                contact, action = ghl.upsert_contact(payload)
                            else:
                                raise

                        total_stats[action] = total_stats.get(action, 0) + 1
                        already_imported.add(fr_id)
                        log_entries.append({
                            "fr_id": fr_id,
                            "ghl_id": contact.get("id") if contact else None,
                            "action": action,
                        })
                        time.sleep(0.3)
                    except Exception as e:
                        total_stats["errors"] += 1
                        err_msg = str(e)
                        log_entries.append({"fr_id": fr_id, "action": "error", "error": err_msg})
                        if total_stats["errors"] <= 3:
                            print(f"  ERROR (fr_id={fr_id}): {err_msg}")

                processed += 1

            # Save checkpoint after each FR batch (every 100 customers)
            if not dry_run:
                checkpoint["imported_fr_ids"] = list(already_imported)
                save_checkpoint(checkpoint)

            if (i + batch_size) % 500 == 0 or i == 0:
                pct = min(100, round((i + batch_size) / max(len(to_import), 1) * 100))
                print(
                    f"    {min(i + batch_size, len(to_import)):,}/{len(to_import):,} ({pct}%) | "
                    f"created: {total_stats['created']} | "
                    f"updated: {total_stats.get('updated', 0)} | "
                    f"errors: {total_stats['errors']}"
                )

            time.sleep(1.5)  # stay under FR's 60-reads/min limit

    # ── Summary ────────────────────────────────────────────────────────────────
    print(f"\n{'=' * 65}")
    print("Import Complete" if not dry_run else "Dry Run Complete")
    print(f"{'=' * 65}")
    print(f"  {'Would create' if dry_run else 'Created'}:    {total_stats['created']:,}")
    if not dry_run:
        print(f"  Updated:     {total_stats.get('updated', 0):,}")
    print(f"  No contact info (skipped): {total_stats['skipped_no_contact']:,}")
    print(f"  Errors:      {total_stats['errors']:,}")

    if not dry_run:
        os.makedirs("logs", exist_ok=True)
        with open(LOG_FILE, "w") as f:
            json.dump(
                {"run_at": datetime.now().isoformat(), "stats": total_stats, "entries": log_entries},
                f,
                indent=2,
            )
        print(f"\n  Full log saved to: {LOG_FILE}")

    if dry_run:
        print(
            "\n  This was a dry run — nothing was changed in GHL.\n"
            "  Run with --live when ready to import."
        )
    else:
        estimated_minutes = total_stats["created"] * 0.25 / 60
        if estimated_minutes > 1:
            print(f"\n  Tip: a full live run takes ~{estimated_minutes:.0f} min for this many contacts.")


if __name__ == "__main__":
    main()
