with ranked as (
    select *, row_number() over (
        partition by firm_id, payment_id order by version desc
    ) as version_rank
    from {{ ref('stg_payment_versions') }}
)
select firm_id, payment_id, version, amount_cents
from ranked
where version_rank = 1
