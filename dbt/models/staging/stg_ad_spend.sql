select spend_date, campaign_id, impressions, clicks, spend_eur
from {{ source('raw', 'ad_spend') }}
