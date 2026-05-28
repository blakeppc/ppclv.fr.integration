"""
FieldRoutes API client — shared by all scripts.

API pattern:
  Search:  GET /api/{endpoint}/search  → returns list of IDs
  Get:     GET /api/{endpoint}/get     → pass {endpoint}IDs=id1,id2 → returns full records
  Update:  POST /api/{endpoint}/update → pass {endpoint}ID + fields to change
  Create:  POST /api/{endpoint}/create → pass fields for new record
"""
import os
import requests
from datetime import datetime


def client_from_env(office_number):
    """
    Create a FieldRoutesClient from environment variables for a given office number.
    Usage: client = client_from_env(1)  or  client_from_env(2)
    """
    n = str(office_number)
    subdomain = os.environ.get(f"FR_OFFICE{n}_SUBDOMAIN", "").strip()
    key = os.environ.get(f"FR_OFFICE{n}_KEY", "").strip()
    token = os.environ.get(f"FR_OFFICE{n}_TOKEN", "").strip()

    if not all([subdomain, key, token]):
        raise ValueError(
            f"Missing credentials for Office {n}. "
            f"Set FR_OFFICE{n}_SUBDOMAIN, FR_OFFICE{n}_KEY, and FR_OFFICE{n}_TOKEN."
        )
    return FieldRoutesClient(subdomain=subdomain, auth_key=key, auth_token=token)


class FieldRoutesClient:
    def __init__(self, subdomain, auth_key, auth_token):
        self.base_url = f"https://{subdomain}.fieldroutes.com/api"
        self.auth_key = auth_key
        self.auth_token = auth_token
        self.session = requests.Session()

    def _auth(self):
        return {"authenticationKey": self.auth_key, "authenticationToken": self.auth_token}

    def _get(self, endpoint, action, params=None):
        url = f"{self.base_url}/{endpoint}/{action}"
        query = self._auth()
        if params:
            query.update(params)
        response = self.session.get(url, params=query)
        response.raise_for_status()
        data = response.json()
        if not data.get("success"):
            raise RuntimeError(f"API error [{endpoint}/{action}]: {data.get('errorMessage', 'Unknown error')}")
        return data

    def _post(self, endpoint, action, payload):
        url = f"{self.base_url}/{endpoint}/{action}"
        body = self._auth()
        body.update(payload)
        response = self.session.post(url, data=body)
        response.raise_for_status()
        data = response.json()
        if not data.get("success"):
            raise RuntimeError(f"API error [{endpoint}/{action}]: {data.get('errorMessage', 'Unknown error')}")
        return data

    # ── Customers ─────────────────────────────────────────────────────────────

    def search_customers(self, filters=None):
        """Returns list of customerIDs matching filters."""
        return self._get("customer", "search", filters)

    def get_customers(self, customer_ids):
        """Fetches full customer records for a list of IDs."""
        ids = ",".join(str(i) for i in customer_ids) if isinstance(customer_ids, list) else str(customer_ids)
        data = self._get("customer", "get", {"customerIDs": ids})
        return data.get("customers", [])

    # ── Subscriptions ─────────────────────────────────────────────────────────

    def search_subscriptions(self, filters=None):
        """Returns list of subscriptionIDs matching filters."""
        return self._get("subscription", "search", filters)

    def get_subscriptions(self, subscription_ids):
        """Fetches full subscription records for a list of IDs."""
        ids = ",".join(str(i) for i in subscription_ids) if isinstance(subscription_ids, list) else str(subscription_ids)
        data = self._get("subscription", "get", {"subscriptionIDs": ids})
        return data.get("subscriptions", [])

    def update_subscription_price(self, subscription_id, new_price):
        """Updates the recurring charge on a subscription."""
        return self._post("subscription", "update", {
            "subscriptionID": subscription_id,
            "recurringCharge": f"{new_price:.2f}",
        })

    # ── Appointments (work orders / completed services) ───────────────────────

    def search_appointments(self, filters=None):
        """
        Returns list of appointmentIDs matching filters.
        Useful date filters:
          dateStart / dateEnd         — scheduled date range (YYYY-MM-DD)
          dateCompletedStart/End      — completion date range
          dateUpdatedStart/End        — last-updated range (use for daily sync)
          officeID                    — limit to one office
          status                      — 0=pending, 1=complete, 2=cancelled, etc.
        """
        return self._get("appointment", "search", filters)

    def get_appointments(self, appointment_ids):
        """Fetches full appointment records for a list of IDs.
        Includes timeIn, timeOut, routeID, sequence, assignedTech, ticketID,
        productionValue, amountCollected, reserviceReasonID, etc.
        """
        ids = ",".join(str(i) for i in appointment_ids) if isinstance(appointment_ids, list) else str(appointment_ids)
        data = self._get("appointment", "get", {"appointmentIDs": ids})
        return data.get("appointments", [])

    # ── Tickets (invoices with line items) ────────────────────────────────────

    def search_tickets(self, filters=None):
        """
        Returns list of ticketIDs matching filters.
        Useful date filters:
          dateStart / dateEnd         — invoice date range (YYYY-MM-DD)
          dateCreatedStart/End        — creation date range
          dateUpdatedStart/End        — last-updated range (use for daily sync)
          officeID                    — limit to one office
          active                      — 1 = active tickets only
        """
        return self._get("ticket", "search", filters)

    def get_tickets(self, ticket_ids):
        """Fetches full ticket records including line items.
        The response includes an 'items' array with productID, quantity, amount
        for each line item — used for chemical usage tracking.
        """
        ids = ",".join(str(i) for i in ticket_ids) if isinstance(ticket_ids, list) else str(ticket_ids)
        data = self._get("ticket", "get", {"ticketIDs": ids})
        return data.get("tickets", [])

    # ── Service Types ─────────────────────────────────────────────────────────

    def search_service_types(self, filters=None):
        """Returns list of service type IDs. Key: 'serviceTypeIDs' in response."""
        return self._get("serviceType", "search", filters)

    def get_service_types(self, type_ids):
        """Fetches full service type records.
        NOTE: GET parameter is 'typeIDs' (not 'serviceTypeIDs').
        Includes reservice, initial, regularService, frequency, defaultCharge.
        """
        ids = ",".join(str(i) for i in type_ids) if isinstance(type_ids, list) else str(type_ids)
        data = self._get("serviceType", "get", {"typeIDs": ids})
        return data.get("serviceTypes", [])

    # ── Employees ─────────────────────────────────────────────────────────────

    def search_employees(self, filters=None):
        """Returns list of employeeIDs matching filters.
        Pass active=1 to get only active employees.
        """
        return self._get("employee", "search", filters)

    def get_employees(self, employee_ids):
        """Fetches full employee records (name, initials, office, type, etc.)."""
        ids = ",".join(str(i) for i in employee_ids) if isinstance(employee_ids, list) else str(employee_ids)
        data = self._get("employee", "get", {"employeeIDs": ids})
        return data.get("employees", [])

    # ── Products (chemicals / materials) ──────────────────────────────────────

    def search_products(self, filters=None):
        """Returns list of productIDs matching filters."""
        return self._get("product", "search", filters)

    def get_products(self, product_ids):
        """Fetches full product records (description, code, category, unit cost)."""
        ids = ",".join(str(i) for i in product_ids) if isinstance(product_ids, list) else str(product_ids)
        data = self._get("product", "get", {"productIDs": ids})
        return data.get("products", [])

    # ── Notes ─────────────────────────────────────────────────────────────────

    def add_note(self, customer_id, note_text):
        """Adds a note to a customer account."""
        return self._post("note", "create", {
            "customerID": customer_id,
            "note": note_text,
            "date": datetime.now().strftime("%Y-%m-%d"),
        })

    # ── Flags / Tags ──────────────────────────────────────────────────────────

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def is_real_record(record):
        """
        Returns True if this record belongs to a real customer/office.
        Filters out orphaned historical records (officeID=-1 or customerID=-1)
        that appear in the API but are excluded from the FieldRoutes UI counts.
        """
        return (
            str(record.get("officeID", "-1")) != "-1"
            and str(record.get("customerID", "-1")) != "-1"
        )

    # ── Flags / Tags ──────────────────────────────────────────────────────────

    def search_flags(self, filters=None):
        """Returns list of all generic flag IDs (customer tags)."""
        return self._get("genericFlag", "search", filters)

    def get_flags(self, flag_ids):
        """Fetches full flag records."""
        ids = ",".join(str(i) for i in flag_ids) if isinstance(flag_ids, list) else str(flag_ids)
        data = self._get("genericFlag", "get", {"genericFlagIDs": ids})
        return data.get("genericFlags", [])

    # ── Legacy aliases (kept for backwards compatibility) ─────────────────────

    def search_invoices(self, filters=None):
        """Alias for search_tickets (legacy name)."""
        return self.search_tickets(filters)

    def get_invoices(self, invoice_ids):
        """Alias for get_tickets (legacy name)."""
        return self.get_tickets(invoice_ids)
