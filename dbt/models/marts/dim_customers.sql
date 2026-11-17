-- acquisition channel/campaign = the session of the customer's first order
with first_order as (
    select customer_id, session_id
    from {{ ref('stg_orders') }}
    qualify row_number() over (partition by customer_id order by ordered_at, order_id) = 1
)
select
    c.customer_id,
    c.visitor_id,
    c.created_at,
    c.country,
    s.channel_id as acquisition_channel_id,
    s.campaign_id as acquisition_campaign_id
from {{ ref('stg_customers') }} c
join first_order f using (customer_id)
join {{ ref('stg_sessions') }} s on s.session_id = f.session_id
