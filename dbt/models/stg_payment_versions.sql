select firm_id, payment_id, version, amount_cents
from {{ source('ledger', 'payments') }}
