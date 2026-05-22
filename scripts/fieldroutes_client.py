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

    # ── Service Orders / Appointments ─────────────────────────────────────────

    def search_service_orders(self, filters=None):
        """Returns list of serviceOrderIDs matching filters."""
        return self._get("serviceOrder", "search", filters)

    def get_service_orders(self, service_order_ids):
        """Fetches full service order records for a list of IDs."""
        ids = ",".join(str(i) for i in service_order_ids) if isinstance(service_order_ids, list) else str(service_order_ids)
        data = self._get("serviceOrder", "get", {"serviceOrderIDs": ids})
        return data.get("serviceOrders", [])

    # ── Notes ─────────────────────────────────────────────────────────────────

    def add_note(self, customer_id, note_text):
        """Adds a note to a customer account."""
        return self._post("note", "create", {
            "customerID": customer_id,
            "note": note_text,
            "date": datetime.now().strftime("%Y-%m-%d"),
        })

    # ── Invoices ──────────────────────────────────────────────────────────────

    def search_invoices(self, filters=None):
        """Returns list of invoiceIDs matching filters."""
        return self._get("invoice", "search", filters)

    def get_invoices(self, invoice_ids):
        """Fetches full invoice records for a list of IDs."""
        ids = ",".join(str(i) for i in invoice_ids) if isinstance(invoice_ids, list) else str(invoice_ids)
        data = self._get("invoice", "get", {"invoiceIDs": ids})
        return data.get("invoices", [])

    # ── Flags / Tags ──────────────────────────────────────────────────────────

    def search_flags(self, filters=None):
        """Returns list of all generic flag IDs (customer tags)."""
        return self._get("genericFlag", "search", filters)

    def get_flags(self, flag_ids):
        """Fetches full flag records."""
        ids = ",".join(str(i) for i in flag_ids) if isinstance(flag_ids, list) else str(flag_ids)
        data = self._get("genericFlag", "get", {"genericFlagIDs": ids})
        return data.get("genericFlags", [])
