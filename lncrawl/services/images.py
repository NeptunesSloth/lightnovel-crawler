from typing import Dict, List, Optional

import sqlmodel as sq

from ..context import ctx
from ..dao import Chapter, ChapterImage
from ..exceptions import ServerErrors


class ChapterImageService:
    def __init__(self) -> None:
        pass

    def list(
        self,
        novel_id: Optional[str] = None,
        chapter_id: Optional[str] = None,
        is_crawled: Optional[bool] = None,
    ) -> List[ChapterImage]:
        with ctx.db.session() as sess:
            stmt = sq.select(ChapterImage)
            if novel_id:
                stmt = stmt.where(ChapterImage.novel_id == novel_id)
            if chapter_id:
                stmt = stmt.where(ChapterImage.chapter_id == chapter_id)
            if is_crawled is not None:
                stmt = stmt.where(sq.col(ChapterImage.is_done).is_(is_crawled))
            items = sess.exec(stmt).all()
            return list(items)

    def list_ids(
        self,
        novel_id: Optional[str] = None,
        chapter_id: Optional[str] = None,
        chapter_ids: Optional[List[str]] = None,
    ) -> List[str]:
        if chapter_ids is not None and not chapter_ids:
            return []
        with ctx.db.session() as sess:
            stmt = sq.select(ChapterImage.id)
            if novel_id:
                stmt = stmt.where(ChapterImage.novel_id == novel_id)
            if chapter_id:
                stmt = stmt.where(ChapterImage.chapter_id == chapter_id)
            if chapter_ids is None:
                return list(sess.exec(stmt).all())

            # Stay below SQLite's variable limit, even for very long novels.
            selected = list(dict.fromkeys(chapter_ids))
            items: List[str] = []
            for offset in range(0, len(selected), 500):
                batch = stmt.where(
                    sq.col(ChapterImage.chapter_id).in_(selected[offset : offset + 500])
                )
                items.extend(sess.exec(batch).all())
            return items

    def get(self, image_id: str) -> ChapterImage:
        with ctx.db.session() as sess:
            image = sess.get(ChapterImage, image_id)
            if not image:
                raise ServerErrors.no_such_file
            return image

    def get_many(self, image_ids: List[str]) -> List[ChapterImage]:
        with ctx.db.session() as sess:
            stmt = sq.select(ChapterImage).where(sq.col(ChapterImage.id).in_(image_ids))
            items = sess.exec(stmt).all()
            return list(items)

    def delete(self, image_id: str) -> bool:
        with ctx.db.session() as sess:
            image = sess.get(ChapterImage, image_id)
            if not image:
                return True
            ctx.files.resolve(image.image_file).unlink(True)
            sess.delete(image)
            sess.commit()
            return True

    def sync(self, chapter: Chapter, images: Dict[str, str]):
        with ctx.db.session() as sess:
            existing = {
                img.id: img
                for img in sess.exec(
                    sq.select(ChapterImage).where(ChapterImage.chapter_id == chapter.id)
                ).all()
            }

            wk = set(images.keys())
            ek = set(existing.keys())
            to_insert = wk - ek
            to_delete = ek - wk

            if to_insert:
                crawler_version = chapter.extra.get("crawler_version")
                sess.exec(
                    sq.insert(ChapterImage),
                    params=[
                        ChapterImage(
                            id=id,
                            url=images[id],
                            chapter_id=chapter.id,
                            novel_id=chapter.novel_id,
                            extra=dict(crawler_version=crawler_version),
                        ).model_dump()
                        for id in to_insert
                    ],
                )

            if to_delete:
                sess.exec(sq.delete(ChapterImage).where(sq.col(ChapterImage.id).in_(to_delete)))
                for id in to_delete:
                    file = existing[id].image_file
                    ctx.files.resolve(file).unlink(True)

            sess.commit()
