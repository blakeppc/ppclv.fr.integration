"""
Creates all BigQuery views used by the Looker Studio dashboard.
Safe to run multiple times — views are replaced in place.

Usage:
  python3 scripts/bq_create_views.py

Views created:
  v_mrr_by_service_type     Current MRR broken down by service plan
  v_monthly_revenue         Revenue trend by month and office
  v_customer_growth         New / cancelled customers by month
  v_tech_production         Production value by tech by month
  v_tech_hourly_rate        $/hr per tech (from timeIn/timeOut)
  v_reservice_rates         Reservice % by tech and by month
  v_ar_aging                Outstanding balances in 30/60/90/90+ day buckets
  v_inspection_conversion   Inspection → recurring service conversion rate
  v_chemical_usage          Products / chemicals used per tech per month
  v_route_analysis_office2  Office 2 upcoming month schedule (route planning)
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


def create_views(bq):
    p = bq.project
    d = bq.dataset
    T = lambda name: f"`{p}.{d}.{name}`"

    views = {

        # ── MRR snapshot ──────────────────────────────────────────────────────
        "v_mrr_by_service_type": f"""
            SELECT
                s.service_type,
                s.office_id,
                CASE s.office_id WHEN 1 THEN 'Residential' WHEN 2 THEN 'Commercial' ELSE 'Unknown' END AS office_name,
                COUNT(*)                       AS subscription_count,
                ROUND(SUM(s.recurring_charge), 2) AS mrr
            FROM {T("dim_subscriptions")} s
            WHERE s.active = TRUE
              AND s.recurring_charge > 0
            GROUP BY 1, 2, 3
        """,

        # ── Monthly revenue trend ─────────────────────────────────────────────
        "v_monthly_revenue": f"""
            SELECT
                DATE_TRUNC(invoice_date, MONTH)  AS month,
                office_id,
                CASE office_id WHEN 1 THEN 'Residential' WHEN 2 THEN 'Commercial' ELSE 'Unknown' END AS office_name,
                COUNT(*)                          AS invoice_count,
                ROUND(SUM(total), 2)              AS revenue,
                ROUND(SUM(balance), 2)            AS outstanding,
                ROUND(AVG(total), 2)              AS avg_invoice
            FROM {T("fact_tickets")}
            WHERE active      = TRUE
              AND invoice_date IS NOT NULL
              AND total        > 0
            GROUP BY 1, 2, 3
        """,

        # ── Customer growth (new / cancelled by month) ────────────────────────
        "v_customer_growth": f"""
            SELECT
                DATE_TRUNC(date_added, MONTH)  AS month,
                office_id,
                CASE office_id WHEN 1 THEN 'Residential' WHEN 2 THEN 'Commercial' ELSE 'Unknown' END AS office_name,
                commercial_account,
                COUNT(*)                        AS new_customers,
                COUNTIF(date_cancelled IS NOT NULL
                    AND DATE_TRUNC(date_cancelled, MONTH) = DATE_TRUNC(date_added, MONTH))
                                                AS same_month_cancel
            FROM {T("dim_customers")}
            WHERE date_added IS NOT NULL
            GROUP BY 1, 2, 3, 4

            UNION ALL

            -- Cancellations that happened in months after signup
            SELECT
                DATE_TRUNC(date_cancelled, MONTH) AS month,
                office_id,
                CASE office_id WHEN 1 THEN 'Residential' WHEN 2 THEN 'Commercial' ELSE 'Unknown' END AS office_name,
                commercial_account,
                0                                  AS new_customers,
                COUNT(*)                           AS same_month_cancel
            FROM {T("dim_customers")}
            WHERE date_cancelled IS NOT NULL
              AND DATE_TRUNC(date_cancelled, MONTH) != DATE_TRUNC(date_added, MONTH)
            GROUP BY 1, 2, 3, 4
        """,

        # ── Tech production by month ──────────────────────────────────────────
        # production_value: explicit when set in FR, otherwise equals revenue
        # revenue_value:    the invoice total (what was actually charged)
        # These differ for warranty/free services and discounted commercial accounts
        "v_tech_production": f"""
            SELECT
                DATE_TRUNC(a.scheduled_date, MONTH)          AS month,
                a.office_id,
                CASE a.office_id WHEN 1 THEN 'Residential' WHEN 2 THEN 'Commercial' ELSE 'Unknown' END AS office_name,
                a.serviced_by_id                              AS employee_id,
                COALESCE(CONCAT(e.first_name, ' ', e.last_name), CAST(a.serviced_by_id AS STRING))
                                                              AS tech_name,
                COALESCE(e.initials, '')                      AS initials,
                COUNT(*)                                      AS completed_jobs,
                ROUND(SUM(
                    CASE WHEN tk.production_value >= 0
                         THEN tk.production_value
                         ELSE tk.total END
                ), 2)                                         AS production_value,
                ROUND(SUM(tk.total), 2)                       AS revenue_value,
                ROUND(AVG(
                    CASE WHEN tk.production_value >= 0
                         THEN tk.production_value
                         ELSE tk.total END
                ), 2)                                         AS avg_job_production,
                ROUND(AVG(tk.total), 2)                       AS avg_job_revenue,
                COUNTIF(st.reservice = TRUE OR a.reservice_reason_id IS NOT NULL)
                                                              AS reservice_count
            FROM {T("fact_appointments")} a
            JOIN {T("fact_tickets")} tk
              ON a.ticket_id = tk.ticket_id
            LEFT JOIN {T("dim_employees")} e
                   ON a.serviced_by_id = e.employee_id
            LEFT JOIN {T("dim_service_types")} st
                   ON a.service_type_id = st.type_id
            WHERE a.status           = 1
              AND a.serviced_by_id  IS NOT NULL
              AND a.scheduled_date  IS NOT NULL
              AND tk.total           > 0
            GROUP BY 1, 2, 3, 4, 5, 6
        """,

        # ── $/hr per tech ─────────────────────────────────────────────────────
        "v_tech_hourly_rate": f"""
            SELECT
                DATE_TRUNC(a.scheduled_date, MONTH)                   AS month,
                a.office_id,
                CASE a.office_id WHEN 1 THEN 'Residential' WHEN 2 THEN 'Commercial' ELSE 'Unknown' END AS office_name,
                a.serviced_by_id                                       AS employee_id,
                COALESCE(CONCAT(e.first_name, ' ', e.last_name), CAST(a.serviced_by_id AS STRING))
                                                                       AS tech_name,
                COALESCE(e.initials, '')                               AS initials,
                COUNT(*)                                               AS jobs_with_time,
                ROUND(SUM(a.duration_actual_min) / 60.0, 2)           AS total_hours_on_site,
                ROUND(SUM(
                    CASE WHEN tk.production_value >= 0
                         THEN tk.production_value ELSE tk.total END
                ), 2)                                                  AS total_production,
                ROUND(SUM(tk.total), 2)                                AS total_revenue,
                ROUND(AVG(a.duration_actual_min), 1)                  AS avg_minutes_per_job,
                CASE WHEN SUM(a.duration_actual_min) > 0
                     THEN ROUND(
                         SUM(CASE WHEN tk.production_value >= 0
                                  THEN tk.production_value ELSE tk.total END)
                         / (SUM(a.duration_actual_min) / 60.0), 2)
                     ELSE NULL END                                     AS production_per_hour,
                CASE WHEN SUM(a.duration_actual_min) > 0
                     THEN ROUND(SUM(tk.total) / (SUM(a.duration_actual_min) / 60.0), 2)
                     ELSE NULL END                                     AS revenue_per_hour
            FROM {T("fact_appointments")} a
            JOIN {T("fact_tickets")} tk
              ON a.ticket_id = tk.ticket_id
            LEFT JOIN {T("dim_employees")} e
                   ON a.serviced_by_id = e.employee_id
            WHERE a.status              = 1
              AND a.duration_actual_min > 0
              AND a.duration_actual_min < 600
              AND a.serviced_by_id     IS NOT NULL
              AND a.scheduled_date     IS NOT NULL
              AND tk.total              > 0
            GROUP BY 1, 2, 3, 4, 5, 6
        """,

        # ── Reservice rates ───────────────────────────────────────────────────
        "v_reservice_rates": f"""
            SELECT
                DATE_TRUNC(a.scheduled_date, MONTH)  AS month,
                a.office_id,
                CASE a.office_id WHEN 1 THEN 'Residential' WHEN 2 THEN 'Commercial' ELSE 'Unknown' END AS office_name,
                a.serviced_by_id                      AS employee_id,
                COALESCE(CONCAT(e.first_name, ' ', e.last_name), CAST(a.serviced_by_id AS STRING))
                                                      AS tech_name,
                COALESCE(e.initials, '')               AS initials,
                a.route_id,
                COUNT(*)                              AS total_completed,
                COUNTIF(st.reservice = TRUE OR a.reservice_reason_id IS NOT NULL)
                                                      AS reservice_count,
                ROUND(100.0 *
                    COUNTIF(st.reservice = TRUE OR a.reservice_reason_id IS NOT NULL)
                    / COUNT(*), 2)                    AS reservice_pct
            FROM {T("fact_appointments")} a
            LEFT JOIN {T("dim_employees")} e
                   ON a.serviced_by_id = e.employee_id
            LEFT JOIN {T("dim_service_types")} st
                   ON a.service_type_id = st.type_id
            WHERE a.status           = 1
              AND a.serviced_by_id  IS NOT NULL
              AND a.scheduled_date  IS NOT NULL
            GROUP BY 1, 2, 3, 4, 5, 6, 7
        """,

        # ── A/R aging ─────────────────────────────────────────────────────────
        "v_ar_aging": f"""
            SELECT
                t.office_id,
                CASE t.office_id WHEN 1 THEN 'Residential' WHEN 2 THEN 'Commercial' ELSE 'Unknown' END AS office_name,
                t.customer_id,
                COALESCE(NULLIF(c.company_name, ''),
                         CONCAT(c.first_name, ' ', c.last_name))      AS customer_name,
                c.phone1,
                c.email,
                t.ticket_id,
                t.invoice_date,
                DATE_DIFF(CURRENT_DATE(), t.invoice_date, DAY)        AS days_outstanding,
                t.total,
                t.balance,
                CASE
                    WHEN DATE_DIFF(CURRENT_DATE(), t.invoice_date, DAY) <=  30 THEN '0-30 days'
                    WHEN DATE_DIFF(CURRENT_DATE(), t.invoice_date, DAY) <=  60 THEN '31-60 days'
                    WHEN DATE_DIFF(CURRENT_DATE(), t.invoice_date, DAY) <=  90 THEN '61-90 days'
                    ELSE '90+ days'
                END                                                     AS aging_bucket,
                CASE
                    WHEN DATE_DIFF(CURRENT_DATE(), t.invoice_date, DAY) <=  30 THEN 1
                    WHEN DATE_DIFF(CURRENT_DATE(), t.invoice_date, DAY) <=  60 THEN 2
                    WHEN DATE_DIFF(CURRENT_DATE(), t.invoice_date, DAY) <=  90 THEN 3
                    ELSE 4
                END                                                     AS aging_sort
            FROM {T("fact_tickets")} t
            LEFT JOIN {T("dim_customers")} c ON t.customer_id = c.customer_id
            WHERE t.balance      > 0.01
              AND t.active       = TRUE
              AND t.invoice_date IS NOT NULL
        """,

        # ── Inspection → conversion rate ──────────────────────────────────────
        "v_inspection_conversion": f"""
            WITH inspections AS (
                SELECT
                    a.appointment_id,
                    a.customer_id,
                    a.office_id,
                    a.scheduled_date    AS inspection_date,
                    st.description      AS inspection_type
                FROM {T("fact_appointments")} a
                JOIN {T("dim_service_types")} st
                  ON a.service_type_id = st.type_id
                WHERE st.is_inspection = TRUE
                  AND a.status         = 1
            ),
            followup AS (
                SELECT DISTINCT a.customer_id, a.scheduled_date
                FROM {T("fact_appointments")} a
                JOIN {T("dim_service_types")} st
                  ON a.service_type_id = st.type_id
                WHERE a.status         = 1
                  AND st.is_inspection = FALSE
                  AND st.reservice     = FALSE
                  AND st.regular_service = TRUE
            )
            SELECT
                DATE_TRUNC(i.inspection_date, MONTH)  AS month,
                i.office_id,
                CASE i.office_id WHEN 1 THEN 'Residential' WHEN 2 THEN 'Commercial' ELSE 'Unknown' END AS office_name,
                i.inspection_type,
                COUNT(*)                               AS total_inspections,
                COUNTIF(f.customer_id IS NOT NULL)     AS converted,
                ROUND(100.0 * COUNTIF(f.customer_id IS NOT NULL) / COUNT(*), 2)
                                                       AS conversion_pct
            FROM inspections i
            LEFT JOIN followup f
                   ON i.customer_id    = f.customer_id
                  AND f.scheduled_date > i.inspection_date
                  AND f.scheduled_date <= DATE_ADD(i.inspection_date, INTERVAL 90 DAY)
            GROUP BY 1, 2, 3, 4
        """,

        # ── Chemical / product usage per tech ─────────────────────────────────
        "v_chemical_usage": f"""
            SELECT
                DATE_TRUNC(t.invoice_date, MONTH)    AS month,
                ti.office_id,
                CASE ti.office_id WHEN 1 THEN 'Residential' WHEN 2 THEN 'Commercial' ELSE 'Unknown' END AS office_name,
                a.serviced_by_id                      AS employee_id,
                COALESCE(CONCAT(e.first_name, ' ', e.last_name), CAST(a.serviced_by_id AS STRING))
                                                      AS tech_name,
                COALESCE(e.initials, '')               AS initials,
                p.product_id,
                p.description                         AS product_name,
                p.code                                AS product_code,
                p.category                            AS product_category,
                ROUND(SUM(ti.quantity), 3)            AS total_quantity,
                ROUND(SUM(ti.amount),   2)            AS total_cost
            FROM {T("fact_ticket_items")} ti
            LEFT JOIN {T("fact_tickets")}     t  ON ti.ticket_id      = t.ticket_id
            LEFT JOIN {T("fact_appointments")} a  ON t.appointment_id  = a.appointment_id
            LEFT JOIN {T("dim_employees")}     e  ON a.serviced_by_id  = e.employee_id
            LEFT JOIN {T("dim_products")}      p  ON ti.product_id     = p.product_id
            WHERE ti.product_id   > 0
              AND t.invoice_date IS NOT NULL
              AND ti.quantity     > 0
            GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10
        """,

        # ── Office 2 route analysis — upcoming month ──────────────────────────
        "v_route_analysis_office2": f"""
            SELECT
                a.scheduled_date,
                a.route_id,
                a.sequence,
                a.assigned_tech_id                    AS employee_id,
                COALESCE(CONCAT(e.first_name, ' ', e.last_name), CAST(a.assigned_tech_id AS STRING))
                                                      AS tech_name,
                COALESCE(e.initials, '')               AS initials,
                a.customer_id,
                COALESCE(NULLIF(c.company_name, ''),
                         CONCAT(c.first_name, ' ', c.last_name))
                                                      AS customer_name,
                c.address,
                c.city,
                c.zip,
                st.description                        AS service_type,
                a.duration_scheduled_min,
                CASE WHEN tk.production_value >= 0
                     THEN tk.production_value ELSE tk.total END AS production_value,
                tk.total                                        AS revenue_value,
                a.appointment_id
            FROM {T("fact_appointments")} a
            LEFT JOIN {T("fact_tickets")} tk
              ON a.ticket_id = tk.ticket_id
            LEFT JOIN {T("dim_employees")}     e  ON a.assigned_tech_id = e.employee_id
            LEFT JOIN {T("dim_customers")}     c  ON a.customer_id      = c.customer_id
            LEFT JOIN {T("dim_service_types")} st ON a.service_type_id  = st.type_id
            WHERE a.office_id       = 2
              AND a.status          = 0    -- pending / scheduled
              AND a.scheduled_date >= DATE_TRUNC(DATE_ADD(CURRENT_DATE(), INTERVAL 1 MONTH), MONTH)
              AND a.scheduled_date <  DATE_TRUNC(DATE_ADD(CURRENT_DATE(), INTERVAL 2 MONTH), MONTH)
            ORDER BY a.route_id, a.scheduled_date, a.sequence
        """,

    }

    print(f"Creating {len(views)} views in {p}.{d}...")
    print()

    ok = 0
    for name, sql in views.items():
        full_sql = f"CREATE OR REPLACE VIEW `{p}.{d}.{name}` AS\n{sql}"
        try:
            bq.query(full_sql)
            print(f"  ✓ {name}")
            ok += 1
        except Exception as e:
            print(f"  ✗ {name} — {e}")

    print()
    print(f"{ok}/{len(views)} views created successfully.")
    if ok == len(views):
        print("\nAll views ready. Next: connect Looker Studio to BigQuery.")
        print("→  https://lookerstudio.google.com")


def main():
    print("=" * 60)
    print("Creating BigQuery Views — FieldRoutes KPI Dashboard")
    print("=" * 60)
    print()
    bq = client_from_env()
    print(f"Project: {bq.project}  |  Dataset: {bq.dataset}\n")
    create_views(bq)


if __name__ == "__main__":
    main()
