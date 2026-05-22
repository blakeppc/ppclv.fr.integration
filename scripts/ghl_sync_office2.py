"""
Daily sync: Office 2 (Preventive Pest Control Commercial) → GoHighLevel.

Pulls all customers updated in FieldRoutes in the last 48 hours and
updates their GHL contact tags and info to match.

Runs automatically via GitHub Actions on a daily schedule.
Can also be run manually:
  python3 scripts/ghl_sync_office2.py
  python3 scripts/ghl_sync_office2.py --days 7    # look back 7 days instead of 2
"""
import os
import sys
import time
import argparse
from datetime import datetime, timedelta

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.fieldroutes_client import client_from_env as fr_from_env
from scripts.ghl_client import client_from_env as ghl_from_env, format_phone

OFFICE_NUM = 2
OFFICE_TAG = "fr_office2"
OFFICE_NAME = "Preventive Pest Control Commercial"


def build_ghl_payload(customer):
    is_active = str(customer.get("active", "0")) == "1"
    is_commercial = str(customer.get("commercialAccount", "0")) == "1"

    phone = format_phone(customer.get("phone1")) or format_phone(customer.get("phone2"))
    email = (customer.get("email") or "").strip().lower() or None

    payload = {
        "firstName": (customer.get("fname") or "").strip(),
        "lastName": (customer.get("lname") or "").strip(),
        "tags": [
            OFFICE_TAG,
            "fr_active" if is_active else "fr_inactive",
            "fr_commercial" if is_commercial else "fr_residential",
        ],
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=2, help="How many days back to look (default: 2)")
    args = parser.parse_args()

    since = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")

    print("=" * 60)
    print(f"GHL Daily Sync — Office {OFFICE_NUM}: {OFFICE_NAME}")
    print(f"Checking for FR changes since: {since}")
    print("=" * 60)

    fr = fr_from_env(OFFICE_NUM)
    ghl = ghl_from_env()

    print("\nFetching recently updated customers from FieldRoutes...")
    result = fr.search_customers({"dateUpdatedStart": since})
    customer_ids = result.get("customerIDs", [])
    print(f"  {len(customer_ids):,} customers updated since {since}")

    if not customer_ids:
        print("  Nothing to sync.")
        return

    stats = {"created": 0, "updated": 0, "skipped_no_contact": 0, "skipped_orphaned": 0, "errors": 0}

    for i in range(0, len(customer_ids), 100):
        batch = customer_ids[i : i + 100]
        try:
            customers = fr.get_customers(batch)
        except Exception as e:
            print(f"  ERROR fetching batch: {e}")
            stats["errors"] += len(batch)
            continue

        for customer in customers:
            if not fr.is_real_record(customer):
                stats["skipped_orphaned"] += 1
                continue

            payload = build_ghl_payload(customer)

            if not payload.get("email") and not payload.get("phone"):
                stats["skipped_no_contact"] += 1
                continue

            try:
                _, action = ghl.upsert_contact(payload)
                stats[action] = stats.get(action, 0) + 1
                time.sleep(0.25)
            except Exception as e:
                fr_id = customer.get("customerID", "?")
                print(f"  ERROR syncing customer {fr_id}: {e}")
                stats["errors"] += 1

        time.sleep(0.05)

    print(f"\n{'─' * 60}")
    print(f"Sync complete")
    print(f"  Created: {stats['created']} | Updated: {stats['updated']} | "
          f"Skipped: {stats['skipped_no_contact']+stats['skipped_orphaned']} | Errors: {stats['errors']}")


if __name__ == "__main__":
    main()
