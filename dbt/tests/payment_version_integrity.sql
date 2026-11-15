select firm_id, payment_id, version
from {{ ref('stg_payment_versions') }}
group by firm_id, payment_id, version
having count(*) <> 1 or min(version) <= 0 or min(amount_cents) < 0
