"""Group access classification, in both languages the bot will actually meet.

Spain is the v1 market, so a Spanish-rendered group is the normal case, not
an edge one. The rule these tests enforce is that a group we simply failed to
read is reported as ``UNKNOWN_ERROR`` -- never as ``UNAVAILABLE`` and never as
empty. A failed extraction is not evidence that a group is gone or quiet.
"""

from __future__ import annotations

from bot.services.facebook.groups import GroupAccess, check_access
from tests.conftest import FakePage

GROUP = "https://www.facebook.com/groups/example"


async def test_english_join_button_means_membership_required() -> None:
    page = FakePage(url=GROUP, texts={"Join Group": 1})
    assert await check_access(page, GROUP) == GroupAccess.MEMBERSHIP_REQUIRED


async def test_spanish_join_button_means_membership_required() -> None:
    page = FakePage(url=GROUP, texts={"Unirse al grupo": 1})
    assert await check_access(page, GROUP) == GroupAccess.MEMBERSHIP_REQUIRED


async def test_spanish_pending_is_not_mistaken_for_membership() -> None:
    page = FakePage(url=GROUP, texts={"Pendiente": 1})
    assert await check_access(page, GROUP) == GroupAccess.PENDING_APPROVAL


async def test_spanish_unavailable() -> None:
    page = FakePage(url=GROUP, texts={"no está disponible": 1})
    assert await check_access(page, GROUP) == GroupAccess.UNAVAILABLE


async def test_unaccented_spanish_still_matches() -> None:
    """Rollouts differ on accents; both spellings are the same button."""
    page = FakePage(url=GROUP, texts={"no esta disponible": 1})
    assert await check_access(page, GROUP) == GroupAccess.UNAVAILABLE


async def test_spanish_search_box_means_accessible() -> None:
    page = FakePage(url=GROUP, placeholders={"Buscar en este grupo": 1})
    assert await check_access(page, GROUP) == GroupAccess.ACCESSIBLE


async def test_spanish_composer_means_accessible() -> None:
    page = FakePage(url=GROUP, texts={"Escribe algo": 1})
    assert await check_access(page, GROUP) == GroupAccess.ACCESSIBLE


async def test_http_404_is_unavailable() -> None:
    page = FakePage(url=GROUP, response_status=404)
    assert await check_access(page, GROUP) == GroupAccess.UNAVAILABLE


async def test_login_form_beats_any_text() -> None:
    """Structural signals are trusted ahead of localized text."""
    page = FakePage(
        url=GROUP,
        selectors={"#login_form, form[data-testid='royal_login_form']": 1},
        texts={"Join Group": 1},
    )
    assert await check_access(page, GROUP) == GroupAccess.LOGIN_REQUIRED


async def test_unreadable_group_is_unknown_not_unavailable() -> None:
    """The distinction the whole design rests on: failure is not absence."""
    page = FakePage(url=GROUP)
    assert await check_access(page, GROUP) == GroupAccess.UNKNOWN_ERROR


async def test_public_group_is_readable_without_joining():
    page = FakePage(texts={"Join Group": 1, "Write something": 1})
    assert await check_access(page, GROUP) == GroupAccess.ACCESSIBLE


async def test_russian_public_group_is_readable_without_joining():
    page = FakePage(texts={"Присоединиться к группе": 1, "Напишите что-нибудь": 1})
    assert await check_access(page, GROUP) == GroupAccess.ACCESSIBLE


async def test_pending_public_group_with_visible_posts_is_readable():
    page = FakePage(texts={"Pending": 1}, selectors={
        "div[role='article'] a[href*='/posts/'], div[role='article'] a[href*='/permalink/']": 1,
    })
    assert await check_access(page, GROUP) == GroupAccess.ACCESSIBLE


async def test_real_post_extracts_message_author_and_clean_permalink():
    from bot.services.facebook.groups import _extract_post
    from tests.conftest import FakeLocator

    # Minimal structural fixture from the observed Russian Facebook group search.
    article = FakeLocator(count=1, text="Person A 2 ч. Listing Like Comment")
    message = FakeLocator(count=1, text="Parcela 4000 m², 65000 EUR Показать меньше")
    article.children["[data-ad-preview='message'], [data-ad-comet-preview='message']"] = message
    article.children["a[href*='/posts/'], a[href*='/permalink/']"] = FakeLocator(
        count=1, attribute="/groups/example/posts/123/?__cft__=tracking", text="2 ч.",
    )
    authors = FakeLocator()
    authors.elements = [
        FakeLocator(count=1, attribute="/groups/example/user/1/"),
        FakeLocator(count=1, attribute="/groups/example/user/1/", text="Person A"),
    ]
    article.children["a[href*='/user/']"] = authors
    more = FakeLocator(count=1)
    message.texts["Ещё"] = more
    post = await _extract_post(article, GROUP)
    assert post is not None
    assert post.post_url == GROUP + "/posts/123/"
    assert post.author == "Person A"
    assert post.text == "Parcela 4000 m², 65000 EUR"
    assert post.posted_at_text == "2 ч."
    assert more.click_calls == 1


async def test_post_permalink_is_canonical_for_absolute_tracking_link():
    """Facebook adds tracking query parameters that must not reach Telegram."""
    from bot.services.facebook.groups import _extract_post
    from tests.conftest import FakeLocator

    article = FakeLocator(count=1)
    article.children["[data-ad-preview='message'], [data-ad-comet-preview='message']"] = (
        FakeLocator(count=1, text="Solar edificable en Madrid")
    )
    article.children["a[href*='/posts/'], a[href*='/permalink/']"] = FakeLocator(
        count=1,
        attribute=(
            "https://www.facebook.com/groups/example/posts/456/"
            "?__cft__[0]=tracking&utm_source=facebook"
        ),
        text="1 h.",
    )

    post = await _extract_post(article, GROUP)

    assert post is not None
    assert post.post_url == GROUP + "/posts/456/"


async def test_cross_group_permalink_is_not_returned_from_group_feed():
    """A cross-post/recommendation must not escape the requested group scope."""
    from bot.services.facebook.groups import _extract_post
    from tests.conftest import FakeLocator

    article = FakeLocator(count=1)
    article.children["[data-ad-preview='message'], [data-ad-comet-preview='message']"] = (
        FakeLocator(count=1, text="Unrelated listing")
    )
    article.children["a[href*='/posts/'], a[href*='/permalink/']"] = FakeLocator(
        count=1,
        attribute="/groups/another-group/posts/456/",
        text="1 h.",
    )

    assert await _extract_post(article, GROUP) is None


async def test_person_search_result_is_not_a_post():
    from bot.services.facebook.groups import _extract_post
    from tests.conftest import FakeLocator

    person = FakeLocator(count=1, text="Person Terreno Добавить в друзья")
    assert await _extract_post(person, GROUP) is None


async def test_read_recent_posts_collects_newly_loaded_posts_and_deduplicates(monkeypatch):
    """The feed reader scrolls only until it has the requested post budget."""
    from unittest.mock import AsyncMock

    from bot.services.facebook import groups
    from tests.conftest import FakeLocator

    page = FakePage()
    articles = FakeLocator()
    articles.elements = [FakeLocator(count=1)]
    page.locators["div[role='article']"] = articles
    first = groups.GroupPost(group_url=GROUP, post_url=GROUP + "/posts/1/", text="First")
    second = groups.GroupPost(group_url=GROUP, post_url=GROUP + "/posts/2/", text="Second")
    extract = AsyncMock(side_effect=[first, first, second])
    monkeypatch.setattr(groups, "_extract_post", extract)

    posts = await groups.read_recent_posts(page, GROUP, max_posts=2)

    assert posts == [first, second]
    assert extract.await_count == 3
    # It scrolled to get there; how far is the scroller's business, and the
    # feed reader and the in-group search now share one.
    assert page.mouse.wheel.await_count == 2


async def test_read_recent_posts_skips_one_unreadable_article(monkeypatch):
    from unittest.mock import AsyncMock

    from bot.services.facebook import groups
    from tests.conftest import FakeLocator

    page = FakePage()
    articles = FakeLocator()
    articles.elements = [FakeLocator(count=1), FakeLocator(count=1)]
    page.locators["div[role='article']"] = articles
    post = groups.GroupPost(group_url=GROUP, post_url=GROUP + "/posts/3/", text="Readable")
    extract = AsyncMock(side_effect=[RuntimeError("missing permalink"), post])
    monkeypatch.setattr(groups, "_extract_post", extract)

    posts = await groups.read_recent_posts(page, GROUP, max_posts=1)

    assert posts == [post]
    assert extract.await_count == 2


async def test_search_opens_russian_group_search_button_and_ignores_people(monkeypatch):
    from unittest.mock import AsyncMock

    from bot.services.facebook import groups
    from tests.conftest import FakeLocator

    page = FakePage(placeholders={"Поиск в этой группе": 1})
    page.roles["Поиск по этой группе"] = FakeLocator(count=1)
    page.locators["div[role='article']"] = FakeLocator(count=1)
    post = groups.GroupPost(group_url=GROUP, post_url=GROUP + "/posts/123/", text="Full text")
    extract = AsyncMock(side_effect=[None, None, post])
    monkeypatch.setattr(groups, "_extract_post", extract)
    result = await groups.search_posts(page, GROUP, "terreno", max_posts=1)
    assert result == [post]
    page.keyboard.press.assert_awaited_once_with("Enter")
    assert not any(call.args == ("networkidle",) for call in page.wait_for_load_state.call_args_list)


async def test_native_group_discovery_returns_named_groups_without_joining():
    from bot.services.facebook.groups import discover_groups
    from tests.conftest import FakeLocator

    page = FakePage()
    links = FakeLocator()
    links.elements = [
        FakeLocator(count=1, attribute="/groups/alpha/", text="Alpha terrenos"),
        FakeLocator(count=1, attribute="/groups/alpha/?__tn__=x", text=""),
        FakeLocator(count=1, attribute="/groups/beta/", text="Beta terrenos"),
        FakeLocator(count=1, attribute="/groups/beta/posts/1/", text="A post"),
    ]
    page.locators["div[role='main'] a[href*='/groups/']"] = links
    found = await discover_groups(page, "terreno Valencia", max_groups=5)
    assert found == [
        ("https://www.facebook.com/groups/alpha/", "Alpha terrenos"),
        ("https://www.facebook.com/groups/beta/", "Beta terrenos"),
    ]
    assert page.goto_calls == [
        "https://www.facebook.com/search/groups/?q=terreno%20Valencia"
    ]
