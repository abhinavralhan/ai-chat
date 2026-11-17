select session_id, visitor_id, started_at, channel_id, campaign_id, device, country
from {{ source('raw', 'sessions') }}
