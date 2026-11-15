select firm_id, payment_id
from {{ ref('current_payments') }}
group by firm_id, payment_id
having count(*) <> 1
