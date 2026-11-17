select
    c.campaign_id,
    c.campaign_name,
    c.channel_id,
    ch.channel_name,
    ch.channel_type,
    c.objective,
    c.start_date,
    c.end_date
from {{ ref('stg_campaigns') }} c
join {{ ref('stg_channels') }} ch using (channel_id)
