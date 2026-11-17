select customer_id, visitor_id, created_at, country
from {{ source('raw', 'customers') }}
