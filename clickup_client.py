import requests
import logging

logger = logging.getLogger(__name__)

BASE = 'https://api.clickup.com/api/v2'

# Task status names (must match exactly what's in ClickUp, lowercase)
STATUS_DEPOSIT_INVOICED = 'deposit invoiced'
STATUS_FINAL_INVOICED   = 'final invoiced'


class ClickUpClient:
    def __init__(self, api_token, list_id):
        self.list_id = list_id
        self.headers = {
            'Authorization': api_token,
            'Content-Type': 'application/json'
        }
        # Cache of field name (lowercase) -> full field object
        self._fields: dict = {}
        self._load_fields()

    # ------------------------------------------------------------------ #
    #  Initialisation                                                      #
    # ------------------------------------------------------------------ #

    def _load_fields(self):
        """Fetch and cache all custom fields from the Jobs list."""
        try:
            resp = requests.get(f'{BASE}/list/{self.list_id}/field', headers=self.headers)
            resp.raise_for_status()
            for field in resp.json().get('fields', []):
                self._fields[field['name'].lower()] = field
            logger.info(f'Loaded {len(self._fields)} custom fields from ClickUp')
        except Exception as e:
            logger.error(f'Failed to load ClickUp custom fields: {e}')

    # ------------------------------------------------------------------ #
    #  Reading tasks                                                       #
    # ------------------------------------------------------------------ #

    def get_task(self, task_id: str) -> dict:
        resp = requests.get(f'{BASE}/task/{task_id}', headers=self.headers)
        resp.raise_for_status()
        return resp.json()

    def parse_custom_fields(self, task: dict) -> dict:
        """
        Return a flat dict of {normalised_field_name: value} from a task.
        Dropdown values are resolved to their option name string.
        """
        result = {}
        for field in task.get('custom_fields', []):
            key   = field['name'].lower().replace(' ', '_').replace('#', 'number')
            value = field.get('value')

            if field['type'] == 'drop_down' and value is not None:
                options = field.get('type_config', {}).get('options', [])
                for opt in options:
                    # ClickUp stores the selected option as its orderindex
                    if str(opt.get('orderindex')) == str(value):
                        value = opt['name']
                        break

            result[key] = value
        return result

    # ------------------------------------------------------------------ #
    #  Writing to tasks                                                    #
    # ------------------------------------------------------------------ #

    def update_status(self, task_id: str, status: str):
        resp = requests.put(
            f'{BASE}/task/{task_id}',
            headers=self.headers,
            json={'status': status}
        )
        resp.raise_for_status()
        logger.info(f'Task {task_id} → status: {status}')

    def set_field(self, task_id: str, field_name: str, value):
        """
        Set a custom field by its display name (case-insensitive).
        Handles text, number, currency, and dropdown field types.
        """
        field = self._fields.get(field_name.lower())
        if not field:
            logger.warning(f'Field "{field_name}" not found in cache — reloading')
            self._load_fields()
            field = self._fields.get(field_name.lower())
        if not field:
            logger.error(f'Field "{field_name}" still not found after reload')
            return

        field_id   = field['id']
        field_type = field['type']

        if field_type == 'drop_down':
            options = field.get('type_config', {}).get('options', [])
            option  = next((o for o in options if o['name'].lower() == str(value).lower()), None)
            if not option:
                logger.error(f'Dropdown option "{value}" not found in field "{field_name}"')
                return
            payload = {'value': option['orderindex']}
        else:
            payload = {'value': value}

        resp = requests.post(
            f'{BASE}/task/{task_id}/field/{field_id}',
            headers=self.headers,
            json=payload
        )
        resp.raise_for_status()
        logger.info(f'Task {task_id} → {field_name}: {value}')

    # ------------------------------------------------------------------ #
    #  Payment tracking (called from Xero webhook)                        #
    # ------------------------------------------------------------------ #

    def mark_invoice_paid(self, task_id: str, invoice_type: str):
        """
        Update the deposit or final invoice status field to 'Paid'.
        invoice_type must be 'deposit' or 'final'.
        """
        field_name = 'Deposit Invoice Status' if invoice_type == 'deposit' else 'Final Invoice Status'
        self.set_field(task_id, field_name, 'Paid')
        logger.info(f'Task {task_id} → {field_name} marked Paid')

    # ------------------------------------------------------------------ #
    #  Contact sync (called from Xero webhook)                            #
    # ------------------------------------------------------------------ #

    def add_client_option(self, contact_name: str) -> bool:
        """
        Add a new option to the Client dropdown field.
        Returns True if added successfully, False if already exists or failed.
        """
        field = self._fields.get('client')
        if not field:
            self._load_fields()
            field = self._fields.get('client')
        if not field:
            logger.error('Client dropdown field not found in ClickUp')
            return False

        # Check if option already exists
        existing = [o['name'].lower() for o in field.get('type_config', {}).get('options', [])]
        if contact_name.lower() in existing:
            logger.info(f'Client "{contact_name}" already exists in dropdown — skipping')
            return False

        # Add the new option via ClickUp API
        field_id = field['id']
        resp = requests.post(
            f'{BASE}/list/{self.list_id}/field/{field_id}/option',
            headers=self.headers,
            json={'name': contact_name}
        )
        if resp.ok:
            logger.info(f'Added "{contact_name}" to Client dropdown')
            self._load_fields()  # Refresh cache so new option is usable immediately
            return True
        else:
            logger.error(f'Failed to add "{contact_name}" to Client dropdown: {resp.status_code} {resp.text}')
            return False
