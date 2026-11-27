from contextlib import contextmanager
from types import SimpleNamespace
from typing import Iterator, List
import unittest
from unittest.mock import patch

import test_novel_repair

from lncrawl.dao import Novel
from lncrawl.services import ranking


class RankingTests(unittest.TestCase):
    """A site's popularity ranking gives each novel of the library its place."""

    def setUp(self) -> None:
        fixture = test_novel_repair.NovelRepairTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.ctx = fixture.ctx
        fixture.stack.enter_context(patch.object(ranking, "ctx", self.ctx))
        with self.ctx.db.session() as sess:
            for slug in ("second-best", "unknown-gem"):
                sess.add(
                    Novel(
                        id=slug,
                        title=slug,
                        url=f"https://www.novelfire.net/book/{slug}/",
                        domain="novelfire.net",
                    )
                )
            sess.commit()
        self.asked: List[str] = []

        def popular() -> Iterator[str]:
            for url in (
                "https://novelfire.net/book/top-novel",
                "https://novelfire.net/book/second-best",
                "https://novelfire.net/book/example",  # the fixture's own novel
                "https://novelfire.net/book/never-reached",
            ):
                self.asked.append(url)
                yield url

        @contextmanager
        def prepare_crawler(user_id: str, url: str) -> Iterator[SimpleNamespace]:
            yield SimpleNamespace(popular=popular)

        self.ctx.crawler = SimpleNamespace(prepare_crawler=prepare_crawler)

    def test_each_novel_gets_its_place_and_the_walk_stops_when_all_are_found(self) -> None:
        with self.ctx.db.session() as sess:
            gem = sess.get(Novel, "unknown-gem")
            assert gem is not None
            sess.delete(gem)  # every novel left is on the list
            sess.commit()
        ranking._rank_site("user", "novelfire.net")
        with self.ctx.db.session() as sess:
            ranks = {n.id: (n.extra or {}).get("rank") for n in sess.exec(ranking.sq.select(Novel))}
        self.assertEqual(ranks, {"second-best": 2, "novel": 3})
        self.assertNotIn("https://novelfire.net/book/never-reached", self.asked)

    def test_a_novel_not_on_the_list_stays_unranked(self) -> None:
        ranking._rank_site("user", "novelfire.net")
        with self.ctx.db.session() as sess:
            gem = sess.get(Novel, "unknown-gem")
            assert gem is not None
            self.assertNotIn("rank", gem.extra or {})

    def test_addresses_match_however_they_are_written(self) -> None:
        self.assertEqual(
            ranking.url_key("https://www.NovelFire.net/book/x/"),
            ranking.url_key("https://novelfire.net/book/x"),
        )
        series = "01J76XY7E9FNDZ1DBBM6PBJPFK"
        self.assertEqual(
            ranking.url_key(f"https://weebcentral.com/series/{series}/One-Piece"),
            ranking.url_key(f"https://weebcentral.com/series/{series}"),
        )
        uid = "32d76d19-8a05-4db0-9fc2-e0b0648fe9d0"
        self.assertEqual(
            ranking.url_key(f"https://mangadex.org/title/{uid}/solo-leveling"),
            ranking.url_key(f"https://mangadex.org/title/{uid}"),
        )


if __name__ == "__main__":
    unittest.main()
