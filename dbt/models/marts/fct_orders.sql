-- gross = every order; net = completed only (refunded/cancelled count as 0)
select
    o.order_id,
    o.customer_id,
    o.visitor_id,
    o.session_id,
    o.ordered_at,
    s.channel_id,
    s.campaign_id,
    s.device,
    s.country,
    o.status,
    o.revenue_eur as gross_revenue_eur,
    case when o.status = 'completed' then o.revenue_eur else 0 end as net_revenue_eur
from {{ ref('stg_orders') }} o
join {{ ref('stg_sessions') }} s using (session_id)
