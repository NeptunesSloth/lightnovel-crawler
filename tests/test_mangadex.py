"""MangaDex chapter lists: one entry per chapter number, read from an upload with pages."""

from types import SimpleNamespace
import unittest

from sources.en.m.mangadex import API_HEADERS, MangaDexCrawler

MANGA = "11111111-2222-3333-4444-555555555555"


def upload(uid: str, number: str, pages: int = 10, external: str = "") -> dict:
    attributes = {"chapter": number, "volume": "1", "title": "", "pages": pages}
    if external:
        attributes["externalUrl"] = external
    return {"id": f"{uid}-0000-0000-0000-000000000000", "attributes": attributes}


class ChapterListTests(unittest.TestCase):
    def read(self, feed: list) -> list:
        def get_json(url: str, headers: dict) -> dict:
            if "/feed?" in url:
                return {"data": feed, "total": len(feed)}
            return {"data": {"attributes": {"title": {"en": "Example"}}}}

        crawler = SimpleNamespace(
            novel_url=f"https://mangadex.org/title/{MANGA}",
            get_json=get_json,
            novel_cover=None,
            novel_author="",
            chapters=[],
            volumes=[],
        )
        MangaDexCrawler.read_novel_info(crawler)
        return crawler.chapters

    def test_an_upload_with_pages_is_read_in_place_of_one_without(self) -> None:
        chapters = self.read(
            [
                upload("aaaaaaaa-0001", "1"),
                upload("aaaaaaaa-0002", "2", external="https://elsewhere.example/2"),
                upload("aaaaaaaa-0003", "2"),
                upload("aaaaaaaa-0004", "3", pages=0),
                upload("aaaaaaaa-0005", "3"),
                upload("aaaaaaaa-0006", "4"),
            ]
        )
        self.assertEqual([c.id for c in chapters], [1, 2, 3, 4])
        self.assertEqual(
            [c.url.rsplit("/", 1)[1][:13] for c in chapters],
            ["aaaaaaaa-0001", "aaaaaaaa-0003", "aaaaaaaa-0005", "aaaaaaaa-0006"],
        )

    def test_the_first_upload_with_pages_is_kept(self) -> None:
        chapters = self.read([upload("aaaaaaaa-0001", "1"), upload("aaaaaaaa-0002", "1")])
        self.assertEqual(len(chapters), 1)
        self.assertIn("aaaaaaaa-0001", chapters[0].url)

    def test_a_chapter_with_no_readable_upload_keeps_its_place(self) -> None:
        # dropping it would move every later chapter to another serial, and the
        # saved chapter files are named by serial
        chapters = self.read(
            [
                upload("aaaaaaaa-0001", "1"),
                upload("aaaaaaaa-0002", "2", external="https://elsewhere.example/2"),
                upload("aaaaaaaa-0003", "3"),
            ]
        )
        self.assertEqual([c.id for c in chapters], [1, 2, 3])
        self.assertIn("aaaaaaaa-0003", chapters[2].url)


class ImageHeaderTests(unittest.TestCase):
    def headers(self, url: str) -> dict:
        return MangaDexCrawler.image_headers(MangaDexCrawler.__new__(MangaDexCrawler), url)

    def test_a_cover_is_asked_for_as_a_plain_client(self) -> None:
        # uploads.mangadex.org sends a browser to a challenge page, not the cover
        headers = self.headers(f"https://uploads.mangadex.org/covers/{MANGA}/x.jpg")
        self.assertEqual(headers["User-Agent"], API_HEADERS["User-Agent"])
        self.assertIn("image/", headers["accept"])

    def test_pages_from_mangadex_at_home_keep_the_usual_headers(self) -> None:
        headers = self.headers("https://abc.def.mangadex.network/data/hash/1.jpg")
        self.assertNotIn("User-Agent", headers)
        self.assertIn("image/", headers["accept"])


class NotReadableHereTests(unittest.TestCase):
    def download(self, chapter: dict) -> list:
        asked: list = []

        def get_json(url: str, headers: dict) -> dict:
            asked.append(url)
            return {
                "baseUrl": "https://x.mangadex.network",
                "chapter": {"hash": "h", "data": ["1.jpg"]},
            }

        MangaDexCrawler.download_chapter_body(SimpleNamespace(get_json=get_json), chapter)
        return asked

    def test_a_chapter_only_on_another_site_fails_without_asking_mangadex(self) -> None:
        chapters = ChapterListTests.read(
            ChapterListTests(), [upload("aaaaaaaa", "1", external="https://kmanga.example/1")]
        )
        chapter = {"url": chapters[0].url, **chapters[0].get_extras()}
        with self.assertRaisesRegex(Exception, "only at https://kmanga.example/1"):
            self.download(chapter)

    def test_an_upload_with_pages_that_took_its_place_is_downloaded(self) -> None:
        stale = {"upload": "aaaaaaaa-0000-0000-0000-000000000000", "at": "https://k.example"}
        chapter = {"url": "https://mangadex.org/chapter/bbbbbbbb-0000-0000-0000-000000000000"}
        self.assertEqual(len(self.download({**chapter, "not_here": stale})), 1)


if __name__ == "__main__":
    unittest.main()
