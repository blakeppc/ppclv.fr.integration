"""
Test FieldRoutes API connection for both offices.
Usage: python3 scripts/test_connection.py
"""
import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.fieldroutes_client import client_from_env

OFFICES = {
    1: {"name": "Preventive Las Vegas", "expected_customers": 50000},  # API returns max 50k IDs; real total ~57k
    2: {"name": "Preventive Pest Control (Commercial)", "expected_customers": 10779},
}


def test_office(office_num, office_info):
    name = office_info["name"]
    print(f"\n{'─' * 60}")
    print(f"Office {office_num}: {name}")
    print(f"{'─' * 60}")

    print(f"  Connecting to ppclv{office_num}.fieldroutes.com...")
    try:
        client = client_from_env(office_num)
        print(f"  Connected: {client.base_url}")
    except ValueError as e:
        print(f"  SKIPPED — credentials not set: {e}")
        return False

    print(f"  Counting customers...")
    try:
        result = client.search_customers()
        count = result.get("count", 0)
        expected = office_info["expected_customers"]
        status = "✓" if count >= expected * 0.9 else "⚠ unexpected count"
        print(f"  {status} {count:,} customers (expected ~{expected:,})")
        usage = result.get("tokenUsage", {})
        limits = result.get("tokenLimits", {})
        print(f"  API reads today: {usage.get('requestsReadToday')} / {limits.get('limitReadRequestsPerDay')}")
    except Exception as e:
        print(f"  ERROR: {e}")
        return False

    print(f"  Counting active subscriptions...")
    try:
        result = client.search_subscriptions({"active": 1})
        sub_count = result.get("count", 0)
        sub_ids = result.get("subscriptionIDs", [])
        print(f"  ✓ {sub_count:,} active subscriptions")

        # Fetch a real sample — skip orphaned records (customerID = -1)
        sample = []
        for chunk_start in range(0, min(200, len(sub_ids)), 50):
            chunk = sub_ids[chunk_start:chunk_start + 50]
            subs = client.get_subscriptions(chunk)
            real = [s for s in subs if s.get("customerID") not in ("-1", -1) and float(s.get("recurringCharge", 0)) > 0]
            if real:
                sample = real
                break

        if sample:
            s = sample[0]
            print(f"    Sample: {s.get('serviceType')} | "
                  f"${float(s.get('recurringCharge', 0)):.2f}/service | "
                  f"Status: {s.get('activeText')} | "
                  f"Next: {s.get('nextService')}")
    except Exception as e:
        print(f"  ERROR: {e}")

    return True


def main():
    print("=" * 60)
    print("FieldRoutes API Connection Test — Both Offices")
    print("=" * 60)

    results = {}
    for num, info in OFFICES.items():
        results[num] = test_office(num, info)

    print(f"\n{'=' * 60}")
    connected = sum(1 for v in results.values() if v)
    print(f"Result: {connected}/2 offices connected")
    if connected < 2:
        print("Add missing credentials to your .env file to connect both offices.")
    else:
        print("Both offices connected. Ready to proceed.")
    print("=" * 60)


if __name__ == "__main__":
    main()
