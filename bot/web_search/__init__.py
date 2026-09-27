"""Website search stage of campaigns: AI queries -> SearXNG -> public pages -> collected_posts.

Runs beside the Facebook stage inside the campaign-runner service
(``bot.campaign.runner.main``). Every URL and every site it meets is remembered
per campaign and globally (migration 021), so a page is fetched at most once.
"""
