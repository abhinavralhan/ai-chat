select
    a.spend_date || '-' || a.campaign_id as ad_spend_id,
    a.spend_date,
    a.campaign_id,
    c.channel_id,
    a.impressions,
    a.clicks,
    a.spend_eur
from {{ ref('stg_ad_spend') }} a
join {{ ref('stg_campaigns') }} c using (campaign_id)
