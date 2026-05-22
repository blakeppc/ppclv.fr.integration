"""
GoHighLevel API client — shared by all GHL sync scripts.
"""
import os
import re
import requests


def client_from_env():
    token = os.environ.get("GHL_API_TOKEN", "").strip()
    location_id = os.environ.get("GHL_LOCATION_ID", "").strip()
    if not all([token, location_id]):
        raise ValueError(
            "Missing GHL_API_TOKEN or GHL_LOCATION_ID. "
            "Set them in your .env file."
        )
    return GHLClient(api_token=token, location_id=location_id)


def format_phone(raw):
    """Convert any US phone string to E.164 (+1XXXXXXXXXX), or return None."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return None


class GHLClient:
    BASE_URL = "https://services.leadconnectorhq.com"

    # FR status tags managed by sync scripts — replaced on each run
    # Other tags (e.g. fr_price_increase_pending) are never touched by sync
    FR_STATUS_TAGS = {"fr_active", "fr_inactive"}

    def __init__(self, api_token, location_id):
        self.location_id = location_id
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_token}",
            "Version": "2021-07-28",
            "Content-Type": "application/json",
        })

    def _get(self, path, params=None):
        url = f"{self.BASE_URL}/{path}"
        resp = self.session.get(url, params=params or {})
        resp.raise_for_status()
        return resp.json()

    def _post(self, path, data):
        url = f"{self.BASE_URL}/{path}"
        resp = self.session.post(url, json=data)
        resp.raise_for_status()
        return resp.json()

    def _put(self, path, data):
        url = f"{self.BASE_URL}/{path}"
        resp = self.session.put(url, json=data)
        resp.raise_for_status()
        return resp.json()

    # ── Custom Fields ──────────────────────────────────────────────────────────

    def get_custom_fields(self):
        data = self._get(f"locations/{self.location_id}/customFields")
        return data.get("customFields", [])

    def ensure_custom_field(self, name, field_key):
        """Return the ID of a TEXT custom field, creating it if needed."""
        for field in self.get_custom_fields():
            if field["fieldKey"] == field_key:
                return field["id"]
        result = self._post(f"locations/{self.location_id}/customFields", {
            "name": name,
            "fieldKey": field_key,
            "dataType": "TEXT",
            "model": "contact",
        })
        cf = result.get("customField") or result
        return cf.get("id")

    # ── Contacts ───────────────────────────────────────────────────────────────

    def find_by_email(self, email):
        if not email:
            return None
        data = self._get("contacts/", {"locationId": self.location_id, "email": email})
        contacts = data.get("contacts", [])
        return contacts[0] if contacts else None

    def find_by_phone(self, phone):
        if not phone:
            return None
        data = self._get("contacts/", {"locationId": self.location_id, "phone": phone})
        contacts = data.get("contacts", [])
        return contacts[0] if contacts else None

    def create_contact(self, payload):
        payload["locationId"] = self.location_id
        return self._post("contacts/", payload).get("contact")

    def update_contact(self, contact_id, payload):
        return self._put(f"contacts/{contact_id}", payload).get("contact")

    def upsert_contact(self, payload):
        """
        Find by email then phone, then create or update.
        When updating, preserves any tags we don't manage (e.g. price increase tags).
        Returns (contact, action) where action is 'created' or 'updated'.
        """
        existing = self.find_by_email(payload.get("email"))
        if not existing:
            existing = self.find_by_phone(payload.get("phone"))

        if existing:
            preserved = [t for t in existing.get("tags", []) if t not in self.FR_STATUS_TAGS]
            new_tags = payload.get("tags", [])
            payload["tags"] = list(dict.fromkeys(preserved + new_tags))
            updated = self.update_contact(existing["id"], payload)
            return updated, "updated"
        else:
            created = self.create_contact(payload)
            return created, "created"
