"""
Daily sync: Office 1 (Preventive Las Vegas) → GoHighLevel.

Pulls all customers updated in FieldRoutes in the last 48 hours and
updates their GHL contact tags and info to match.

Runs automatically via GitHub Actions on a daily schedule.
Can also be run manually:
  python3 scripts/ghl_sync_office1.py
  python3 scripts/ghl_sync_office1.py --days 7    # look back 7 days instead of 2
"""
import os
import sys
import time
import argparse
import collections
from datetime import datetime, timedelta

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.fieldroutes_client import client_from_env as fr_from_env
from scripts.ghl_client import client_from_env as ghl_from_env, format_phone

OFFICE_NUM = 1
OFFICE_TAG = "fr_office1"
OFFICE_NAME = "Las Vegas Residential"


def is_sub_active(sub):
    return str(sub.get("active", "0")).strip().lower() in ("1", "true")


def service_tags(subs):
    """Map a customer's FieldRoutes subscriptions to GHL marketing tags (Office 1).

    Inspections and estimates never count. Rodent baiting counts whether the sub is
    active OR inactive (Blake wants everyone who ever had it); every other bucket
    counts active subscriptions only.
    """
    tags = set()
    for s in subs:
        name = (s.get("serviceType") or "").strip().lower()
        if not name:
            continue
        if "inspection" in name or "estimate" in name:
            continue  # never tag inspections/estimates

        # Rodent baiting — active OR inactive.
        if "rodent baiting" in name:
            tags.add("svc_rodent_baiting")
            continue

        if not is_sub_active(s):
            continue  # every remaining bucket is active-only

        if name == "termite treatment":                       # exact only (not wood/final grade)
            tags.add("svc_termite")
        elif "mosquito" in name:
            tags.add("svc_mosquito")
        elif "rodent" in name and any(k in name for k in ("exclusion", "trapping", "control program")):
            tags.add("svc_rodent_service")
        elif "weed" in name:
            tags.add("svc_weed")
        elif "pigeon" in name or "flock" in name:
            tags.add("svc_bird")
        elif "heater rental" in name:                          # checked before bed bug
            tags.add("svc_heater_rental")
        elif "bed bug" in name or "actisol" in name:
            tags.add("svc_bedbug")
        elif "deep root" in name:
            tags.add("svc_deep_root")
        elif "bee" in name:
            tags.add("svc_bee")
        elif ("pest control" in name or "roach" in name
              or "attic" in name or "blacklight" in name or "drain" in name):
            tags.add("svc_pest")
    return tags


def subs_by_customer(fr, customer_ids):
    """Fetch all subscriptions for a batch of customers, grouped by customerID (str)."""
    out = {}
    res = fr.search_subscriptions({"customerIDs": ",".join(str(c) for c in customer_ids)})
    sub_ids = res.get("subscriptionIDs", []) if isinstance(res, dict) else []
    for k in range(0, len(sub_ids), 100):
        for s in fr.get_subscriptions(sub_ids[k:k + 100]):
            out.setdefault(str(s.get("customerID")), []).append(s)
    return out


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
    svc_counts = collections.Counter()   # per-tag totals, for verification
    pest_not_mosquito = 0

    for i in range(0, len(customer_ids), 100):
        batch = customer_ids[i : i + 100]
        try:
            customers = fr.get_customers(batch)
        except Exception as e:
            print(f"  ERROR fetching batch: {e}")
            stats["errors"] += len(batch)
            continue

        try:
            subs_map = subs_by_customer(fr, batch)
        except Exception as e:
            print(f"  WARNING: subscription fetch failed for batch ({e}); no service tags this batch")
            subs_map = {}

        for customer in customers:
            if not fr.is_real_record(customer):
                stats["skipped_orphaned"] += 1
                continue

            payload = build_ghl_payload(customer)

            if not payload.get("email") and not payload.get("phone"):
                stats["skipped_no_contact"] += 1
                continue

            svc = service_tags(subs_map.get(str(customer.get("customerID")), []))
            if svc:
                payload["tags"] = payload["tags"] + sorted(svc)
                svc_counts.update(svc)
                if "svc_pest" in svc and "svc_mosquito" not in svc:
                    pest_not_mosquito += 1

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
    print(f"  Service tags applied (this run's customers):")
    for tag in sorted(svc_counts):
        print(f"    {tag}: {svc_counts[tag]}")
    print(f"    (svc_pest AND NOT svc_mosquito: {pest_not_mosquito})")


if __name__ == "__main__":
    main()
