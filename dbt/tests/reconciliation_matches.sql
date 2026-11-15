-- A failing test is intentional before corrections or for missing controls.
select * from {{ ref('reconciliation') }} where status <> 'matched'
