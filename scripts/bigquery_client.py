"""
BigQuery client — shared by all BQ data scripts.

Handles authentication, table references, and batch row loading.
"""
import os
import json
from datetime import datetime, timezone
from google.cloud import bigquery


def client_from_env():
    """Create a BigQuery client from environment variables."""
    key_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    project = os.environ.get("GCP_PROJECT_ID", "").strip()
    dataset = os.environ.get("BQ_DATASET", "fieldroutes").strip()

    if not project:
        raise ValueError("Missing GCP_PROJECT_ID environment variable.")

    # If a key file path is given, set it for the Google SDK
    if key_path and os.path.exists(key_path):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = os.path.abspath(key_path)

    client = bigquery.Client(project=project)
    return BQClient(client=client, project=project, dataset=dataset)


class BQClient:
    # Inspection service type names (used to flag dim_service_types)
    INSPECTION_NAMES = {
        "Rodent Inspection",
        "Pigeon Inspection",
        "Bed Bug Inspection",
        "Bed Bug Room Inspection",
        "Pest Control Inspection",
    }

    def __init__(self, client, project, dataset):
        self.client = client
        self.project = project
        self.dataset = dataset

    def table_ref(self, table_name):
        return f"{self.project}.{self.dataset}.{table_name}"

    def load_rows(self, table_name, rows, write_disposition="WRITE_APPEND"):
        """
        Load a list of dicts into a BigQuery table.
        Uses load_table_from_json (batch, free tier, fast).
        """
        if not rows:
            return 0

        table = self.table_ref(table_name)
        job_config = bigquery.LoadJobConfig(
            write_disposition=write_disposition,
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        )

        job = self.client.load_table_from_json(rows, table, job_config=job_config)
        job.result()  # wait for completion

        if job.errors:
            raise RuntimeError(f"BQ load errors for {table_name}: {job.errors}")

        return len(rows)

    def overwrite_table(self, table_name, rows):
        """Replace all rows in a dimension table."""
        return self.load_rows(table_name, rows, write_disposition="WRITE_TRUNCATE")

    def table_exists(self, table_name):
        try:
            self.client.get_table(self.table_ref(table_name))
            return True
        except Exception:
            return False

    def query(self, sql):
        return list(self.client.query(sql).result())

    @staticmethod
    def now_utc():
        return datetime.now(timezone.utc).isoformat()

    # ── Row builders ──────────────────────────────────────────────────────────

    def service_type_row(self, st):
        name = st.get("description", "")
        return {
            "type_id": int(st.get("typeID", 0)),
            "description": name,
            "office_id": int(st.get("officeID", -1)),
            "reservice": st.get("reservice") == "1",
            "initial": st.get("initial") == "1",
            "regular_service": st.get("regularService") == "1",
            "frequency": int(st.get("frequency", -1)),
            "billing_frequency": int(st.get("billingFrequency", -1)),
            "default_charge": float(st.get("defaultCharge") or 0),
            "category": st.get("category", ""),
            "is_inspection": name in self.INSPECTION_NAMES,
            "loaded_at": self.now_utc(),
        }

    def employee_row(self, emp):
        return {
            "employee_id": int(emp.get("employeeID", 0)),
            "first_name": (emp.get("fname") or "").strip(),
            "last_name": (emp.get("lname") or "").strip(),
            "nickname": (emp.get("nickname") or "").strip(),
            "initials": (emp.get("initials") or "").strip(),
            "office_id": int(emp.get("officeID", -1)),
            "emp_type": int(emp.get("type", 0)),
            "active": emp.get("active") == "1",
            "email": (emp.get("email") or "").strip(),
            "loaded_at": self.now_utc(),
        }

    def product_row(self, p):
        return {
            "product_id": int(p.get("productID", 0)),
            "description": (p.get("description") or "").strip(),
            "code": (p.get("code") or "").strip(),
            "category": (p.get("category") or "").strip(),
            "unit_cost": float(p.get("amount") or 0),
            "office_id": int(p.get("officeID", -1)),
            "taxable": p.get("taxable") == "1",
            "loaded_at": self.now_utc(),
        }

    def customer_row(self, c):
        def _date(val):
            if not val or str(val).startswith("0000"):
                return None
            return str(val)[:10]

        return {
            "customer_id": int(c.get("customerID", 0)),
            "first_name": (c.get("fname") or "").strip(),
            "last_name": (c.get("lname") or "").strip(),
            "company_name": (c.get("companyName") or "").strip(),
            "email": (c.get("email") or "").strip().lower(),
            "phone1": (c.get("phone1") or "").strip(),
            "address": (c.get("address") or "").strip(),
            "city": (c.get("city") or "").strip(),
            "state": (c.get("state") or "").strip(),
            "zip": (c.get("zip") or "").strip(),
            "office_id": int(c.get("officeID", -1)),
            "active": str(c.get("active", "0")) == "1",
            "commercial_account": str(c.get("commercialAccount", "0")) == "1",
            "date_added": _date(c.get("dateAdded")),
            "date_cancelled": _date(c.get("dateCancelled")),
            "source": (c.get("source") or "").strip(),
            "loaded_at": self.now_utc(),
        }

    def subscription_row(self, s):
        def _date(val):
            if not val or str(val).startswith("0000"):
                return None
            return str(val)[:10]

        return {
            "subscription_id": int(s.get("subscriptionID", 0)),
            "customer_id": int(s.get("customerID", 0)),
            "office_id": int(s.get("officeID", -1)),
            "service_type_id": int(s.get("serviceID", 0)),
            "service_type": (s.get("serviceType") or "").strip(),
            "recurring_charge": float(s.get("recurringCharge") or 0),
            "initial_charge": float(s.get("initialCharge") or 0),
            "frequency": int(s.get("frequency", 0)),
            "next_service": _date(s.get("nextService")),
            "last_service": _date(s.get("lastService")),
            "date_added": _date(s.get("dateAdded")),
            "date_cancelled": _date(s.get("dateCancelled")),
            "active": str(s.get("active", "0")) == "1",
            "loaded_at": self.now_utc(),
        }

    def appointment_row(self, a):
        def _ts(val):
            if not val or str(val).startswith("0000"):
                return None
            return str(val).replace(" ", "T")

        def _date(val):
            if not val or str(val).startswith("0000"):
                return None
            return str(val)[:10]

        def _float(val, default=None):
            try:
                return float(val) if val is not None else default
            except (TypeError, ValueError):
                return default

        def _int(val, default=None):
            try:
                return int(val) if val is not None else default
            except (TypeError, ValueError):
                return default

        # Calculate actual duration from timeIn/timeOut
        duration_actual = None
        try:
            if a.get("timeIn") and a.get("timeOut") and not str(a.get("timeIn","")).startswith("0000"):
                from datetime import datetime as dt
                tin = dt.strptime(str(a["timeIn"])[:19], "%Y-%m-%d %H:%M:%S")
                tout = dt.strptime(str(a["timeOut"])[:19], "%Y-%m-%d %H:%M:%S")
                diff = (tout - tin).total_seconds() / 60
                if 0 < diff < 600:  # sanity check: 0-10 hours
                    duration_actual = round(diff, 2)
        except Exception:
            pass

        additional = a.get("additionalTechs")
        if isinstance(additional, list):
            additional = ",".join(str(x) for x in additional) if additional else None
        elif additional:
            additional = str(additional)

        return {
            "appointment_id": int(a.get("appointmentID", 0)),
            "customer_id": _int(a.get("customerID")),
            "subscription_id": _int(a.get("subscriptionID")),
            "ticket_id": _int(a.get("ticketID")),
            "office_id": _int(a.get("officeID")),
            "route_id": _int(a.get("routeID")),
            "sequence": _int(a.get("sequence")),
            "assigned_tech_id": _int(a.get("assignedTech")),
            "serviced_by_id": _int(a.get("servicedBy")),
            "additional_tech_ids": additional,
            "service_type_id": _int(a.get("type")),
            "scheduled_date": _date(a.get("date")),
            "date_completed": _ts(a.get("dateCompleted")),
            "time_in": _ts(a.get("timeIn")),
            "time_out": _ts(a.get("timeOut")),
            "duration_scheduled_min": _int(a.get("duration")),
            "duration_actual_min": duration_actual,
            "status": _int(a.get("status")),
            "status_text": (a.get("statusText") or "").strip(),
            "is_initial": a.get("isInitial") == "1",
            "production_value": _float(a.get("productionValue")),
            "amount_collected": _float(a.get("amountCollected")),
            "reservice_reason_id": _int(a.get("reserviceReasonID")),
            "lat_in": _float(a.get("latIn")),
            "lng_in": _float(a.get("longIn")),
            "temperature": _float(a.get("temperature")),
            "wind_speed": _float(a.get("windSpeed")),
            "wind_direction": (a.get("windDirection") or "").strip(),
            "date_added": _ts(a.get("dateAdded")),
            "date_updated": _ts(a.get("dateUpdated")),
            "loaded_at": self.now_utc(),
        }

    def ticket_row(self, t):
        def _ts(val):
            if not val or str(val).startswith("0000"):
                return None
            return str(val).replace(" ", "T")

        def _date(val):
            if not val or str(val).startswith("0000"):
                return None
            return str(val)[:10]

        return {
            "ticket_id": int(t.get("ticketID", 0)),
            "appointment_id": int(t.get("appointmentID") or 0),
            "customer_id": int(t.get("customerID", 0)),
            "subscription_id": int(t.get("subscriptionID") or 0),
            "office_id": int(t.get("officeID", -1)),
            "service_id": int(t.get("serviceID") or 0),
            "invoice_date": _date(t.get("invoiceDate")),
            "date_created": _ts(t.get("dateCreated")),
            "date_updated": _ts(t.get("dateUpdated")),
            "subtotal": float(t.get("subTotal") or 0),
            "tax_amount": float(t.get("taxAmount") or 0),
            "total": float(t.get("total") or 0),
            "balance": float(t.get("balance") or 0),
            "production_value": float(t.get("productionValue") or 0),
            "active": t.get("active") == "1",
            "loaded_at": self.now_utc(),
        }

    def ticket_item_rows(self, t):
        """Returns one row per line item in a ticket."""
        rows = []
        office_id = int(t.get("officeID", -1))
        ticket_id = int(t.get("ticketID", 0))
        for item in t.get("items", []):
            try:
                product_id = int(item.get("productID") or 0)
                rows.append({
                    "item_id": int(item.get("itemID", 0)),
                    "ticket_id": ticket_id,
                    "office_id": office_id,
                    "description": (item.get("description") or "").strip(),
                    "quantity": float(item.get("quantity") or 0),
                    "amount": float(item.get("amount") or 0),
                    "product_id": product_id,
                    "service_id": int(item.get("serviceID") or 0),
                    "taxable": item.get("taxable") == "1",
                    "loaded_at": self.now_utc(),
                })
            except Exception:
                pass
        return rows
