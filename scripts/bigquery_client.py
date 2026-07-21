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

    # ── Shared type-safe helpers ──────────────────────────────────────────────
    # The FR API sometimes returns "" (empty string) for numeric fields.
    # bare int("") / float("") crash with ValueError — these helpers return a
    # safe default instead so a single bad record never kills the whole batch.

    @staticmethod
    def _int(val, default=None):
        """int() that gracefully handles None, '', and non-numeric strings."""
        if val is None or val == "":
            return default
        try:
            return int(val)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _float(val, default=None):
        """float() that gracefully handles None, '', and non-numeric strings."""
        if val is None or val == "":
            return default
        try:
            return float(val)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _date(val):
        """Return YYYY-MM-DD string or None; filters out 0000-00-00 placeholders."""
        if not val or str(val).startswith("0000"):
            return None
        return str(val)[:10]

    @staticmethod
    def _ts(val):
        """Return ISO-ish timestamp string or None; filters out 0000-... placeholders."""
        if not val or str(val).startswith("0000"):
            return None
        return str(val).replace(" ", "T")

    # ── Row builders ──────────────────────────────────────────────────────────

    def service_type_row(self, st):
        name = st.get("description", "")
        return {
            "type_id":           self._int(st.get("typeID"), 0),
            "description":       name,
            "office_id":         self._int(st.get("officeID"), -1),
            "reservice":         st.get("reservice") == "1",
            "initial":           st.get("initial") == "1",
            "regular_service":   st.get("regularService") == "1",
            "frequency":         self._int(st.get("frequency"), -1),
            "billing_frequency": self._int(st.get("billingFrequency"), -1),
            "default_charge":    self._float(st.get("defaultCharge"), 0),
            "category":          st.get("category", ""),
            "is_inspection":     name in self.INSPECTION_NAMES,
            "loaded_at":         self.now_utc(),
        }

    def employee_row(self, emp):
        return {
            "employee_id": self._int(emp.get("employeeID"), 0),
            "first_name":  (emp.get("fname") or "").strip(),
            "last_name":   (emp.get("lname") or "").strip(),
            "nickname":    (emp.get("nickname") or "").strip(),
            "initials":    (emp.get("initials") or "").strip(),
            "office_id":   self._int(emp.get("officeID"), -1),
            "emp_type":    self._int(emp.get("type"), 0),
            "active":      emp.get("active") == "1",
            "email":       (emp.get("email") or "").strip(),
            "loaded_at":   self.now_utc(),
        }

    def product_row(self, p):
        return {
            "product_id":  self._int(p.get("productID"), 0),
            "description": (p.get("description") or "").strip(),
            "code":        (p.get("code") or "").strip(),
            "category":    (p.get("category") or "").strip(),
            "unit_cost":   self._float(p.get("amount"), 0),
            "office_id":   self._int(p.get("officeID"), -1),
            "taxable":     p.get("taxable") == "1",
            "loaded_at":   self.now_utc(),
        }

    def customer_row(self, c):
        return {
            "customer_id":       self._int(c.get("customerID"), 0),
            "first_name":        (c.get("fname") or "").strip(),
            "last_name":         (c.get("lname") or "").strip(),
            "company_name":      (c.get("companyName") or "").strip(),
            "email":             (c.get("email") or "").strip().lower(),
            "phone1":            (c.get("phone1") or "").strip(),
            "address":           (c.get("address") or "").strip(),
            "city":              (c.get("city") or "").strip(),
            "state":             (c.get("state") or "").strip(),
            "zip":               (c.get("zip") or "").strip(),
            "office_id":         self._int(c.get("officeID"), -1),
            "active":            str(c.get("active", "0")) == "1",
            "commercial_account":str(c.get("commercialAccount", "0")) == "1",
            "date_added":        self._date(c.get("dateAdded")),
            "date_cancelled":    self._date(c.get("dateCancelled")),
            "source":            (c.get("source") or "").strip(),
            "bill_to_account_id":self._int(c.get("billToAccountID"), 0),
            "auto_pay":          str(c.get("aPay", "No")).strip().lower() == "yes",
            "loaded_at":         self.now_utc(),
        }

    def subscription_row(self, s):
        # FR returns "CUSTOM" (a string) for subscriptions on a non-fixed schedule.
        # These are typically billed monthly at a flat rate — the recurringCharge
        # represents the monthly amount, not a per-visit amount.
        # Store as -2 so views can distinguish CUSTOM from "unknown" (0 / -1).
        raw_freq = s.get("frequency")
        if str(raw_freq).strip().upper() == "CUSTOM":
            frequency = -2   # sentinel: flat monthly billing, charge is already monthly
        else:
            frequency = self._int(raw_freq, 0)

        return {
            "subscription_id": self._int(s.get("subscriptionID"), 0),
            "customer_id":     self._int(s.get("customerID"), 0),
            "office_id":       self._int(s.get("officeID"), -1),
            "service_type_id": self._int(s.get("serviceID"), 0),
            "service_type":    (s.get("serviceType") or "").strip(),
            "recurring_charge":self._float(s.get("recurringCharge"), 0),
            "initial_charge":  self._float(s.get("initialCharge"), 0),
            "frequency":       frequency,
            "next_service":    self._date(s.get("nextService")),
            "last_service":    self._date(s.get("lastService")),
            "date_added":      self._date(s.get("dateAdded")),
            "date_cancelled":  self._date(s.get("dateCancelled")),
            "active":          str(s.get("active", "0")) == "1",
            "loaded_at":       self.now_utc(),
        }

    def appointment_row(self, a, route_tech_map=None):
        # Calculate actual duration from timeIn/timeOut
        duration_actual = None
        try:
            if a.get("timeIn") and a.get("timeOut") and not str(a.get("timeIn", "")).startswith("0000"):
                from datetime import datetime as dt
                tin  = dt.strptime(str(a["timeIn"])[:19],  "%Y-%m-%d %H:%M:%S")
                tout = dt.strptime(str(a["timeOut"])[:19], "%Y-%m-%d %H:%M:%S")
                diff = (tout - tin).total_seconds() / 60
                if 0 < diff < 600:  # sanity check: 0–10 hours
                    duration_actual = round(diff, 2)
        except Exception:
            pass

        additional = a.get("additionalTechs")
        if isinstance(additional, list):
            additional = ",".join(str(x) for x in additional) if additional else None
        elif additional:
            additional = str(additional)

        # Resolve the route's assigned tech. The appointment's own assignedTech
        # is often 0/unset for pending appointments (FieldRoutes only backfills
        # it onto the appointment later, sometimes not until completion), so the
        # route-level assignment is the reliable source of who's actually running
        # the job. Stored separately from assigned_tech_id to preserve the raw
        # appointment field; downstream should COALESCE(NULLIF(assigned_tech_id,0),
        # route_tech_id) to get the best available tech.
        route_tech_id = None
        if route_tech_map:
            route_tech_id = route_tech_map.get(self._int(a.get("routeID")))

        return {
            "appointment_id":        self._int(a.get("appointmentID"), 0),
            "customer_id":           self._int(a.get("customerID")),
            "subscription_id":       self._int(a.get("subscriptionID")),
            "ticket_id":             self._int(a.get("ticketID")),
            "office_id":             self._int(a.get("officeID")),
            "route_id":              self._int(a.get("routeID")),
            "sequence":              self._int(a.get("sequence")),
            "assigned_tech_id":      self._int(a.get("assignedTech")),
            "route_tech_id":         route_tech_id,
            "serviced_by_id":        self._int(a.get("servicedBy")),
            "additional_tech_ids":   additional,
            "service_type_id":       self._int(a.get("type")),
            "scheduled_date":        self._date(a.get("date")),
            "date_completed":        self._ts(a.get("dateCompleted")),
            "time_in":               self._ts(a.get("timeIn")),
            "time_out":              self._ts(a.get("timeOut")),
            "duration_scheduled_min":self._int(a.get("duration")),
            "duration_actual_min":   duration_actual,
            "status":                self._int(a.get("status")),
            "status_text":           (a.get("statusText") or "").strip(),
            "is_initial":            a.get("isInitial") == "1",
            "production_value":      self._float(a.get("productionValue")),
            "amount_collected":      self._float(a.get("amountCollected")),
            "reservice_reason_id":   self._int(a.get("reserviceReasonID")),
            "lat_in":                self._float(a.get("latIn")),
            "lng_in":                self._float(a.get("longIn")),
            "temperature":           self._float(a.get("temperature")),
            "wind_speed":            self._float(a.get("windSpeed")),
            "wind_direction":        (a.get("windDirection") or "").strip(),
            "date_added":            self._ts(a.get("dateAdded")),
            "date_updated":          self._ts(a.get("dateUpdated")),
            "loaded_at":             self.now_utc(),
        }

    def ticket_row(self, t):
        return {
            "ticket_id":        self._int(t.get("ticketID"), 0),
            "appointment_id":   self._int(t.get("appointmentID"), 0),
            "customer_id":      self._int(t.get("customerID"), 0),
            "subscription_id":  self._int(t.get("subscriptionID"), 0),
            "office_id":        self._int(t.get("officeID"), -1),
            "service_id":       self._int(t.get("serviceID"), 0),
            "invoice_date":     self._date(t.get("invoiceDate")),
            "date_created":     self._ts(t.get("dateCreated")),
            "date_updated":     self._ts(t.get("dateUpdated")),
            "subtotal":         self._float(t.get("subTotal"), 0),
            "tax_amount":       self._float(t.get("taxAmount"), 0),
            "total":            self._float(t.get("total"), 0),
            "balance":          self._float(t.get("balance"), 0),
            "production_value": self._float(t.get("productionValue"), 0),
            "active":           t.get("active") == "1",
            "loaded_at":        self.now_utc(),
        }

    def ticket_item_rows(self, t):
        """Returns one row per line item in a ticket."""
        rows = []
        office_id = self._int(t.get("officeID"), -1)
        ticket_id = self._int(t.get("ticketID"), 0)
        for item in t.get("items", []):
            try:
                rows.append({
                    "item_id":     self._int(item.get("itemID"), 0),
                    "ticket_id":   ticket_id,
                    "office_id":   office_id,
                    "description": (item.get("description") or "").strip(),
                    "quantity":    self._float(item.get("quantity"), 0),
                    "amount":      self._float(item.get("amount"), 0),
                    "product_id":  self._int(item.get("productID"), 0),
                    "service_id":  self._int(item.get("serviceID"), 0),
                    "taxable":     item.get("taxable") == "1",
                    "loaded_at":   self.now_utc(),
                })
            except Exception:
                pass
        return rows
