select campaign_id, campaign_name, channel_id, objective, start_date, end_date
from {{ source('raw', 'campaigns') }}
