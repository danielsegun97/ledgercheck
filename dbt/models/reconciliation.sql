with actual as (
    select firm_id, sum(cast(amount_cents as hugeint)) as actual_cents
    from {{ ref('current_payments') }}
    group by firm_id
), controls as (
    select firm_id, expected_cents from {{ source('ledger', 'controls') }}
)
select
    coalesce(a.firm_id, c.firm_id) as firm_id,
    coalesce(a.actual_cents, 0) as actual_cents,
    c.expected_cents,
    coalesce(a.actual_cents, 0) - c.expected_cents as difference_cents,
    case
        when c.firm_id is null then 'unmapped'
        when a.firm_id is null then 'missing_payments'
        when a.actual_cents = c.expected_cents then 'matched'
        else 'mismatch'
    end as status
from actual a
full outer join controls c on a.firm_id = c.firm_id
