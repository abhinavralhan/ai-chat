select order_id, customer_id, visitor_id, session_id, ordered_at, revenue_eur, status
from {{ source('raw', 'orders') }}
