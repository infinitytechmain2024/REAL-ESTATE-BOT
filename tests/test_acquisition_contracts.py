"""P1.1: the social contract shared by Facebook, Instagram, TikTok and LinkedIn (no network, no live calls).

Post and profile links below follow the public link shapes of each network; the handles and ids are made up.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from bot.acquisition.contracts import AcquiredPost, Provenance
from bot.acquisition.people import FB_HOSTS, Engagement, profile_link
from bot.campaign import leads
from bot.campaign.fblinks import FB_HOSTS as CAMPAIGN_FB_HOSTS

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
FB_GROUP = "https://www.facebook.com/groups/pisosmadrid"
FB_POST = "https://www.facebook.com/groups/pisosmadrid/posts/1234567890123/"


def source(**kw) -> Provenance:
    data = {"provider": "apify", "actor_id": "owner/actor", "run_id": "aBc123", "group_url": FB_GROUP,
            "post_url": FB_POST, "collected_at": NOW}
    return Provenance(**{**data, **kw})


def post(**kw) -> AcquiredPost:
    return AcquiredPost(campaign_id="c1", source=source(**kw), raw_text="Vendo parcela de 2.000 m² en Boadilla")


# --- Provenance.platform ---------------------------------------------------------------------------------------


def test_old_facebook_rows_stay_valid_and_default_to_facebook() -> None:
    # The shape the FB mapper and post_leads wrote before v3: no platform key at all (extra="forbid" stays on).
    old = {"schema_version": "1", "campaign_id": "c1", "raw_text": "Vendo piso",
           "source": {"provider": "apify", "actor_id": "apify/facebook-groups-scraper", "run_id": "aBc123",
                      "group_url": FB_GROUP, "post_url": FB_POST, "published_at": None,
                      "collected_at": NOW.isoformat()}}
    parsed = AcquiredPost.model_validate(old)
    assert parsed.source.platform == "facebook"
    assert parsed.platform_post_id() == "1234567890123"
    assert Provenance(provider="local", group_url=FB_GROUP, post_url=FB_POST, collected_at=NOW).platform == "facebook"
    assert AcquiredPost.model_validate(parsed.model_dump(mode="json")) == parsed


def test_platform_is_closed_and_extra_keys_still_refused() -> None:
    with pytest.raises(ValidationError):
        source(platform="x")
    with pytest.raises(ValidationError):
        source(network="instagram")


def test_collected_row_carries_the_platform() -> None:
    assert post().collected_post_row()["raw_payload"]["platform"] == "facebook"
    row = post(platform="tiktok", group_url="https://www.tiktok.com/tag/inversion",
               post_url="https://www.tiktok.com/@casas.madrid/video/7301234567890123456").collected_post_row()
    assert row["raw_payload"]["platform"] == "tiktok"
    assert row["platform_post_id"] == "7301234567890123456"


@pytest.mark.parametrize(("platform", "url", "expected"), [
    ("facebook", FB_POST, "1234567890123"),
    ("facebook", "https://www.facebook.com/permalink.php?story_fbid=987654321&id=1", "987654321"),
    ("instagram", "https://www.instagram.com/p/C9xYz12AbCd/", "C9xYz12AbCd"),
    ("instagram", "https://www.instagram.com/reel/DA1b2C3d4E5/?igsh=abc", "DA1b2C3d4E5"),
    ("instagram", "https://www.instagram.com/reels/DA1b2C3d4E5/", "DA1b2C3d4E5"),
    ("instagram", "https://www.instagram.com/tv/B_x-y_z1234/", "B_x-y_z1234"),
    ("instagram", "https://www.instagram.com/casas.madrid/p/C9xYz12AbCd/", "C9xYz12AbCd"),
    ("tiktok", "https://www.tiktok.com/@casas.madrid/video/7301234567890123456?is_from_webapp=1", "7301234567890123456"),
    ("tiktok", "https://m.tiktok.com/@casas.madrid/photo/7301234567890123457", "7301234567890123457"),
    ("linkedin", "https://www.linkedin.com/posts/ana-perez-inversiones_inmobiliaria-activity-7251234567890123456-AbCd",
     "7251234567890123456"),
    ("linkedin", "https://www.linkedin.com/posts/acme-capital_nueva-promocion-ugcPost-7251234567890123999-x1Yz/", "7251234567890123999"),
    ("linkedin", "https://www.linkedin.com/feed/update/urn:li:activity:7251234567890123456/", "7251234567890123456"),
    ("linkedin", "https://www.linkedin.com/feed/update/urn%3Ali%3Ashare%3A7251234567890123458", "7251234567890123458"),
])
def test_post_id_by_platform(platform: str, url: str, expected: str) -> None:
    assert post(platform=platform, post_url=url).platform_post_id() == expected


@pytest.mark.parametrize(("platform", "url"), [
    ("instagram", "https://www.instagram.com/casas.madrid/"),                       # a profile, not a post
    ("instagram", "https://www.facebook.com/p/C9xYz12AbCd/"),                       # another network's host
    ("tiktok", "https://www.tiktok.com/@casas.madrid"),
    ("tiktok", "https://www.tiktok.com/tag/inversion"),
    ("linkedin", "https://www.linkedin.com/in/ana-perez/"),
    ("instagram", FB_POST),                                                         # the FB regex is FB only
])
def test_post_without_an_id_is_hashed(platform: str, url: str) -> None:
    assert post(platform=platform, post_url=url).platform_post_id().startswith("url:")


# --- profile_link ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("platform", "href", "expected"), [
    ("facebook", "https://www.facebook.com/groups/123/user/100012345678/?__cft__[0]=x",
     ("fb:100012345678", "https://www.facebook.com/profile.php?id=100012345678")),
    ("facebook", "https://www.facebook.com/Olga.Invest?__tn__=R", ("fb:olga.invest", "https://www.facebook.com/Olga.Invest")),
    ("instagram", "https://www.instagram.com/Casas.Madrid/?hl=es", ("ig:casas.madrid", "https://www.instagram.com/casas.madrid")),
    ("instagram", "https://instagram.com/ana_perez", ("ig:ana_perez", "https://www.instagram.com/ana_perez")),
    ("tiktok", "https://www.tiktok.com/@Casas.Madrid?lang=es", ("tt:casas.madrid", "https://www.tiktok.com/@casas.madrid")),
    ("tiktok", "https://m.tiktok.com/@ana_perez/", ("tt:ana_perez", "https://www.tiktok.com/@ana_perez")),
    ("linkedin", "https://es.linkedin.com/in/Ana-Perez-12ab34/?originalSubdomain=es",
     ("li:in:ana-perez-12ab34", "https://www.linkedin.com/in/ana-perez-12ab34")),
    ("linkedin", "https://www.linkedin.com/company/acme-capital/",
     ("li:company:acme-capital", "https://www.linkedin.com/company/acme-capital")),
])
def test_profile_link_per_network(platform: str, href: str, expected: tuple[str, str]) -> None:
    assert profile_link(platform, href) == expected
    assert profile_link(platform, expected[1]) == expected  # canonical is a fixed point


@pytest.mark.parametrize(("platform", "href"), [
    ("instagram", "https://www.instagram.com/p/C9xYz12AbCd/"),          # a post page
    ("instagram", "https://www.instagram.com/reel/DA1b2C3d4E5/"),
    ("instagram", "https://www.instagram.com/explore/tags/inversion/"),
    ("instagram", "https://www.instagram.com/casas.madrid/p/C9xYz12AbCd/"),
    ("instagram", "https://www.tiktok.com/@casas.madrid"),             # another host
    ("instagram", "https://instagram.com.evil.example/casas"),
    ("tiktok", "https://www.tiktok.com/@casas.madrid/video/7301234567890123456"),
    ("tiktok", "https://www.tiktok.com/casas.madrid"),                 # no @: not a profile
    ("tiktok", "https://www.tiktok.com/tag/inversion"),
    ("tiktok", "https://www.instagram.com/@casas.madrid"),
    ("linkedin", "https://www.linkedin.com/posts/ana-perez_x-activity-7251234567890123456-AbCd"),
    ("linkedin", "https://www.linkedin.com/in/ana-perez/details/experience/"),
    ("linkedin", "https://www.linkedin.com/company/acme-capital/posts/"),
    ("linkedin", "https://www.linkedin.com/school/ie-business-school/"),
    ("linkedin", "https://www.linkedin.com.evil.example/in/ana-perez"),
    ("linkedin", "ftp://www.linkedin.com/in/ana-perez"),
    ("facebook", "https://www.instagram.com/casas.madrid"),
    ("facebook", "https://www.facebook.com/groups/123/posts/456/"),
    ("twitter", "https://x.com/casas"),                                # unknown network
    ("instagram", None),
])
def test_profile_link_refuses_what_is_not_a_profile(platform: str, href: object) -> None:
    assert profile_link(platform, href) is None


@pytest.mark.parametrize("href", [
    "https://www.facebook.com/groups/123/user/100012345678/", "https://m.facebook.com/profile.php?id=100099&__tn__=R",
    "https://www.facebook.com/people/Juan-Perez/100077777/", "https://www.facebook.com/Olga.Invest",
    "https://www.facebook.com/groups/123/", "https://www.facebook.com/hashtag/terreno", "https://example.com/x", None,
])
def test_campaign_wrapper_is_the_facebook_branch(href: object) -> None:
    assert leads.profile_link(href) == profile_link("facebook", href)


def test_facebook_hosts_match_the_campaign_copy() -> None:
    # people.py keeps its own copy (bot/acquisition is copied into images without bot/campaign): the two must agree.
    assert FB_HOSTS == CAMPAIGN_FB_HOSTS


# --- Engagement ----------------------------------------------------------------------------------------------------

IG_POST = "https://www.instagram.com/p/C9xYz12AbCd/"


def test_engagement_comment_and_reaction() -> None:
    comment = Engagement(platform="instagram", kind="comment", profile_key="ig:ana_perez",
                         profile_url="https://www.instagram.com/ana_perez", text="¿Precio?", post_url=IG_POST,
                         raw_id="18000000000000001")
    assert comment.author is None and comment.reaction_type is None  # IG gives no name: none is guessed
    reaction = Engagement(platform="linkedin", kind="reaction", author="Ana Pérez", profile_key="li:in:ana-perez",
                          profile_url="https://www.linkedin.com/in/ana-perez", reaction_type="LIKE",
                          post_url="https://www.linkedin.com/feed/update/urn:li:activity:7251234567890123456/")
    assert reaction.text is None


@pytest.mark.parametrize("kw", [
    {"profile_key": "ig:other"},                                      # key does not match the url
    {"profile_url": "https://www.instagram.com/Ana_Perez"},            # not canonical
    {"platform": "tiktok"},                                           # an IG profile under TikTok
    {"text": None},                                                   # a comment without text
    {"reaction_type": "like"},                                        # a comment with a reaction type
    {"kind": "repost"},                                               # a repost with text
    {"post_url": "instagram.com/p/x"},
    {"text": ""},
    {"email": "a@b.c"},                                               # extra keys refused
])
def test_engagement_refuses_inconsistent_rows(kw: dict) -> None:
    data = {"platform": "instagram", "kind": "comment", "profile_key": "ig:ana_perez",
            "profile_url": "https://www.instagram.com/ana_perez", "text": "hola", "post_url": IG_POST}
    with pytest.raises(ValidationError):
        Engagement(**{**data, **kw})
