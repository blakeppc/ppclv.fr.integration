"""
Creates all BigQuery tables for the FieldRoutes KPI dashboard.
Safe to run multiple times — skips tables that already exist.

Usage:
  python3 scripts/bq_setup_tables.py

Tables created:
  Dimension tables (full refresh weekly):
    dim_service_types   — service plan types, reservice flags, inspection flags
    dim_employees       — technicians and staff
    dim_products        — chemicals and materials
    dim_customers       — all customers (active + cancelled)
    dim_subscriptions   — active subscriptions (current MRR state)

  Fact tables (5-year history + daily append):
    fact_appointments   — every work order: time in/out, tech, route, production
    fact_tickets        — every invoice: totals, balance, service link
    fact_ticket_items   — every invoice line item: chemicals, services, charges
"""
import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.bigquery_client import client_from_env
from google.cloud import bigquery

# ── Schema definitions ────────────────────────────────────────────────────────
# Each field: (name, type, nullable)
# nullable=True → mode="NULLABLE" (can be NULL)
# nullable=False → mode="REQUIRED" (for primary key fields only)

SCHEMAS = {

    "dim_service_types": [
        bigquery.SchemaField("type_id",           "INTEGER", mode="REQUIRED"),
        bigquery.SchemaField("description",        "STRING",  mode="NULLABLE"),
        bigquery.SchemaField("office_id",          "INTEGER", mode="NULLABLE"),
        bigquery.SchemaField("reservice",          "BOOLEAN", mode="NULLABLE"),
        bigquery.SchemaField("initial",            "BOOLEAN", mode="NULLABLE"),
        bigquery.SchemaField("regular_service",    "BOOLEAN", mode="NULLABLE"),
        bigquery.SchemaField("frequency",          "INTEGER", mode="NULLABLE"),
        bigquery.SchemaField("billing_frequency",  "INTEGER", mode="NULLABLE"),
        bigquery.SchemaField("default_charge",     "FLOAT",   mode="NULLABLE"),
        bigquery.SchemaField("category",           "STRING",  mode="NULLABLE"),
        bigquery.SchemaField("is_inspection",      "BOOLEAN", mode="NULLABLE"),
        bigquery.SchemaField("loaded_at",          "TIMESTAMP", mode="NULLABLE"),
    ],

    "dim_employees": [
        bigquery.SchemaField("employee_id",  "INTEGER", mode="REQUIRED"),
        bigquery.SchemaField("first_name",   "STRING",  mode="NULLABLE"),
        bigquery.SchemaField("last_name",    "STRING",  mode="NULLABLE"),
        bigquery.SchemaField("nickname",     "STRING",  mode="NULLABLE"),
        bigquery.SchemaField("initials",     "STRING",  mode="NULLABLE"),
        bigquery.SchemaField("office_id",    "INTEGER", mode="NULLABLE"),
        bigquery.SchemaField("emp_type",     "INTEGER", mode="NULLABLE"),
        bigquery.SchemaField("active",       "BOOLEAN", mode="NULLABLE"),
        bigquery.SchemaField("email",        "STRING",  mode="NULLABLE"),
        bigquery.SchemaField("loaded_at",    "TIMESTAMP", mode="NULLABLE"),
    ],

    "dim_products": [
        bigquery.SchemaField("product_id",   "INTEGER", mode="REQUIRED"),
        bigquery.SchemaField("description",  "STRING",  mode="NULLABLE"),
        bigquery.SchemaField("code",         "STRING",  mode="NULLABLE"),
        bigquery.SchemaField("category",     "STRING",  mode="NULLABLE"),
        bigquery.SchemaField("unit_cost",    "FLOAT",   mode="NULLABLE"),
        bigquery.SchemaField("office_id",    "INTEGER", mode="NULLABLE"),
        bigquery.SchemaField("taxable",      "BOOLEAN", mode="NULLABLE"),
        bigquery.SchemaField("loaded_at",    "TIMESTAMP", mode="NULLABLE"),
    ],

    "dim_customers": [
        bigquery.SchemaField("customer_id",        "INTEGER", mode="REQUIRED"),
        bigquery.SchemaField("first_name",          "STRING",  mode="NULLABLE"),
        bigquery.SchemaField("last_name",           "STRING",  mode="NULLABLE"),
        bigquery.SchemaField("company_name",        "STRING",  mode="NULLABLE"),
        bigquery.SchemaField("email",               "STRING",  mode="NULLABLE"),
        bigquery.SchemaField("phone1",              "STRING",  mode="NULLABLE"),
        bigquery.SchemaField("address",             "STRING",  mode="NULLABLE"),
        bigquery.SchemaField("city",                "STRING",  mode="NULLABLE"),
        bigquery.SchemaField("state",               "STRING",  mode="NULLABLE"),
        bigquery.SchemaField("zip",                 "STRING",  mode="NULLABLE"),
        bigquery.SchemaField("office_id",           "INTEGER", mode="NULLABLE"),
        bigquery.SchemaField("active",              "BOOLEAN", mode="NULLABLE"),
        bigquery.SchemaField("commercial_account",  "BOOLEAN", mode="NULLABLE"),
        bigquery.SchemaField("date_added",          "DATE",    mode="NULLABLE"),
        bigquery.SchemaField("date_cancelled",      "DATE",    mode="NULLABLE"),
        bigquery.SchemaField("source",              "STRING",  mode="NULLABLE"),
        bigquery.SchemaField("loaded_at",           "TIMESTAMP", mode="NULLABLE"),
    ],

    "dim_subscriptions": [
        bigquery.SchemaField("subscription_id",   "INTEGER", mode="REQUIRED"),
        bigquery.SchemaField("customer_id",        "INTEGER", mode="NULLABLE"),
        bigquery.SchemaField("office_id",          "INTEGER", mode="NULLABLE"),
        bigquery.SchemaField("service_type_id",    "INTEGER", mode="NULLABLE"),
        bigquery.SchemaField("service_type",       "STRING",  mode="NULLABLE"),
        bigquery.SchemaField("recurring_charge",   "FLOAT",   mode="NULLABLE"),
        bigquery.SchemaField("initial_charge",     "FLOAT",   mode="NULLABLE"),
        bigquery.SchemaField("frequency",          "INTEGER", mode="NULLABLE"),
        bigquery.SchemaField("next_service",       "DATE",    mode="NULLABLE"),
        bigquery.SchemaField("last_service",       "DATE",    mode="NULLABLE"),
        bigquery.SchemaField("date_added",         "DATE",    mode="NULLABLE"),
        bigquery.SchemaField("date_cancelled",     "DATE",    mode="NULLABLE"),
        bigquery.SchemaField("active",             "BOOLEAN", mode="NULLABLE"),
        bigquery.SchemaField("loaded_at",          "TIMESTAMP", mode="NULLABLE"),
    ],

    "fact_appointments": [
        bigquery.SchemaField("appointment_id",          "INTEGER", mode="REQUIRED"),
        bigquery.SchemaField("customer_id",              "INTEGER", mode="NULLABLE"),
        bigquery.SchemaField("subscription_id",          "INTEGER", mode="NULLABLE"),
        bigquery.SchemaField("ticket_id",                "INTEGER", mode="NULLABLE"),
        bigquery.SchemaField("office_id",                "INTEGER", mode="NULLABLE"),
        bigquery.SchemaField("route_id",                 "INTEGER", mode="NULLABLE"),
        bigquery.SchemaField("sequence",                 "INTEGER", mode="NULLABLE"),
        bigquery.SchemaField("assigned_tech_id",         "INTEGER", mode="NULLABLE"),
        bigquery.SchemaField("serviced_by_id",           "INTEGER", mode="NULLABLE"),
        bigquery.SchemaField("additional_tech_ids",      "STRING",  mode="NULLABLE"),
        bigquery.SchemaField("service_type_id",          "INTEGER", mode="NULLABLE"),
        bigquery.SchemaField("scheduled_date",           "DATE",    mode="NULLABLE"),
        bigquery.SchemaField("date_completed",           "TIMESTAMP", mode="NULLABLE"),
        bigquery.SchemaField("time_in",                  "TIMESTAMP", mode="NULLABLE"),
        bigquery.SchemaField("time_out",                 "TIMESTAMP", mode="NULLABLE"),
        bigquery.SchemaField("duration_scheduled_min",   "INTEGER", mode="NULLABLE"),
        bigquery.SchemaField("duration_actual_min",      "FLOAT",   mode="NULLABLE"),
        bigquery.SchemaField("status",                   "INTEGER", mode="NULLABLE"),
        bigquery.SchemaField("status_text",              "STRING",  mode="NULLABLE"),
        bigquery.SchemaField("is_initial",               "BOOLEAN", mode="NULLABLE"),
        bigquery.SchemaField("production_value",         "FLOAT",   mode="NULLABLE"),
        bigquery.SchemaField("amount_collected",         "FLOAT",   mode="NULLABLE"),
        bigquery.SchemaField("reservice_reason_id",      "INTEGER", mode="NULLABLE"),
        bigquery.SchemaField("lat_in",                   "FLOAT",   mode="NULLABLE"),
        bigquery.SchemaField("lng_in",                   "FLOAT",   mode="NULLABLE"),
        bigquery.SchemaField("temperature",              "FLOAT",   mode="NULLABLE"),
        bigquery.SchemaField("wind_speed",               "FLOAT",   mode="NULLABLE"),
        bigquery.SchemaField("wind_direction",           "STRING",  mode="NULLABLE"),
        bigquery.SchemaField("date_added",               "TIMESTAMP", mode="NULLABLE"),
        bigquery.SchemaField("date_updated",             "TIMESTAMP", mode="NULLABLE"),
        bigquery.SchemaField("loaded_at",                "TIMESTAMP", mode="NULLABLE"),
    ],

    "fact_tickets": [
        bigquery.SchemaField("ticket_id",          "INTEGER", mode="REQUIRED"),
        bigquery.SchemaField("appointment_id",      "INTEGER", mode="NULLABLE"),
        bigquery.SchemaField("customer_id",         "INTEGER", mode="NULLABLE"),
        bigquery.SchemaField("subscription_id",     "INTEGER", mode="NULLABLE"),
        bigquery.SchemaField("office_id",           "INTEGER", mode="NULLABLE"),
        bigquery.SchemaField("service_id",          "INTEGER", mode="NULLABLE"),
        bigquery.SchemaField("invoice_date",        "DATE",    mode="NULLABLE"),
        bigquery.SchemaField("date_created",        "TIMESTAMP", mode="NULLABLE"),
        bigquery.SchemaField("date_updated",        "TIMESTAMP", mode="NULLABLE"),
        bigquery.SchemaField("subtotal",            "FLOAT",   mode="NULLABLE"),
        bigquery.SchemaField("tax_amount",          "FLOAT",   mode="NULLABLE"),
        bigquery.SchemaField("total",               "FLOAT",   mode="NULLABLE"),
        bigquery.SchemaField("balance",             "FLOAT",   mode="NULLABLE"),
        bigquery.SchemaField("production_value",    "FLOAT",   mode="NULLABLE"),
        bigquery.SchemaField("active",              "BOOLEAN", mode="NULLABLE"),
        bigquery.SchemaField("loaded_at",           "TIMESTAMP", mode="NULLABLE"),
    ],

    "fact_ticket_items": [
        bigquery.SchemaField("item_id",      "INTEGER", mode="REQUIRED"),
        bigquery.SchemaField("ticket_id",    "INTEGER", mode="NULLABLE"),
        bigquery.SchemaField("office_id",    "INTEGER", mode="NULLABLE"),
        bigquery.SchemaField("description",  "STRING",  mode="NULLABLE"),
        bigquery.SchemaField("quantity",     "FLOAT",   mode="NULLABLE"),
        bigquery.SchemaField("amount",       "FLOAT",   mode="NULLABLE"),
        bigquery.SchemaField("product_id",   "INTEGER", mode="NULLABLE"),
        bigquery.SchemaField("service_id",   "INTEGER", mode="NULLABLE"),
        bigquery.SchemaField("taxable",      "BOOLEAN", mode="NULLABLE"),
        bigquery.SchemaField("loaded_at",    "TIMESTAMP", mode="NULLABLE"),
    ],
}


def create_tables(bq):
    """Create all tables that don't already exist. Returns counts."""
    created = 0
    already_exist = 0

    for table_name, schema in SCHEMAS.items():
        table_ref = bq.table_ref(table_name)
        if bq.table_exists(table_name):
            print(f"  ✓ {table_name} — already exists")
            already_exist += 1
        else:
            table = bigquery.Table(table_ref, schema=schema)
            bq.client.create_table(table)
            print(f"  + {table_name} — created")
            created += 1

    return created, already_exist


def main():
    print("=" * 60)
    print("BigQuery Table Setup — FieldRoutes KPI Dashboard")
    print("=" * 60)

    print(f"\nConnecting to BigQuery...")
    bq = client_from_env()
    print(f"  Project:  {bq.project}")
    print(f"  Dataset:  {bq.dataset}")

    print(f"\nCreating tables ({len(SCHEMAS)} total)...")
    created, existing = create_tables(bq)

    print(f"\n{'─' * 60}")
    print(f"  Created:       {created}")
    print(f"  Already exist: {existing}")
    print(f"  Total tables:  {len(SCHEMAS)}")

    if created == 0 and existing == len(SCHEMAS):
        print("\nAll tables already exist — setup is complete.")
    elif created > 0:
        print(f"\n{created} new table(s) created. Ready for backfill.")
        print("Next step:  python3 scripts/bq_backfill.py")


if __name__ == "__main__":
    main()
