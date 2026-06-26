import re
import shutil
from typing import Any, Dict, List, Optional

import sqlmodel as sq

from ..context import ctx
from ..dao import LanguageCode, Novel, NovelTranslation
from ..exceptions import ServerErrors
from ..server.models import Paginated


def _norm(text: str) -> str:
    """Normalize a title for matching across sources."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


class NovelService:
    def __init__(self) -> None:
        pass

    def list(
        self,
        search: str = "",
        offset: int = 0,
        limit: int = 20,
        domain: str = "",
    ) -> Paginated[Novel]:
        with ctx.db.session() as sess:
            stmt = sq.select(Novel)
            cnt = sq.select(sq.func.count()).select_from(Novel)

            # Apply filters
            conditions: List[Any] = []

            if domain:
                conditions.append(sq.col(Novel.url).ilike(f"%{domain}%"))

            if search:
                conditions.append(sq.col(Novel.title).ilike(f"%{search}%"))

            if conditions:
                cnd = sq.and_(*conditions)
                stmt = stmt.where(cnd)
                cnt = cnt.where(cnd)

            # Apply sorting
            stmt = stmt.order_by(sq.desc(Novel.updated_at))

            # Apply pagination
            stmt = stmt.offset(offset).limit(limit)

            total = sess.exec(cnt).one()
            items = sess.exec(stmt).all()

            return Paginated(
                total=total,
                offset=offset,
                limit=limit,
                items=list(items),
            )

    def list_titles(self, exclude_domain: Optional[str] = None) -> List[str]:
        """All novel titles in the library, optionally excluding one domain.

        Used for cross-source de-duplication so the same story isn't downloaded
        again from a different site.
        """
        with ctx.db.session() as sess:
            stmt = sq.select(Novel.title)
            if exclude_domain:
                stmt = stmt.where(Novel.domain != exclude_domain)
            return [t for t in sess.exec(stmt).all() if t]

    def list_domains(self) -> Dict[str, int]:
        with ctx.db.session() as sess:
            domains = sess.exec(
                sq.select(
                    Novel.domain,
                    sq.func.count(sq.col(Novel.id)).label("total_novels"),
                ).group_by(Novel.domain)
            ).all()
        return {domain: total_novels for domain, total_novels in domains}

    def get(self, novel_id: str, language: Optional[LanguageCode] = None) -> Novel:
        with ctx.db.session() as sess:
            novel = sess.get(Novel, novel_id)
            if not novel:
                raise ServerErrors.no_such_novel
        if language:
            translation = self.get_novel_translation(novel, language)
            if not translation:
                raise ServerErrors.no_such_novel.with_extra(language)
            novel.title = translation.title
            novel.authors = translation.authors
            novel.synopsis = translation.synopsis
        return novel

    def list_translation_languages(self, novel_id: str) -> List[LanguageCode]:
        with ctx.db.session() as sess:
            translations = sess.exec(
                sq.select(NovelTranslation.language).where(
                    NovelTranslation.novel_id == novel_id,
                )
            ).all()
            return [LanguageCode(lang) for lang in translations]

    def get_novel_translation(self, novel: Novel, language: LanguageCode):
        with ctx.db.session() as sess:
            return sess.exec(
                sq.select(NovelTranslation)
                .where(
                    NovelTranslation.novel_id == novel.id,
                    NovelTranslation.language == language,
                )
                .limit(1)
            ).first()

    def delete(self, novel_id: str) -> bool:
        novel_dir = ctx.files.resolve(f"novels/{novel_id}")
        shutil.rmtree(novel_dir, True)
        with ctx.db.session() as sess:
            novel = sess.get(Novel, novel_id)
            if not novel:
                return True
            novel_title = novel.title
            sess.delete(novel)
            sess.commit()
        ctx.recommendations.invalidate(novel_id)
        ctx.recommendations.index_remove(novel_id, novel_title)
        return True

    def find_by_url(self, novel_url: str) -> Optional[Novel]:
        with ctx.db.session() as sess:
            return sess.exec(sq.select(Novel).where(Novel.url == novel_url)).first()

    def heal_from_library(self, novel_id: str) -> Dict[str, Any]:
        """Fill a novel's missing chapters from another copy of the same title.

        Cross-source completeness, the safe way: it looks for other novels in the
        library whose (normalized) title matches, and copies a downloaded chapter
        into a gap ONLY when the chapter titles also match — so it never guesses an
        alignment, never overwrites existing content, and can't corrupt the novel.
        Text chapters only (manga chapters reference per-source image ids, so they
        are skipped). Returns a summary dict.
        """
        novel = self.get(novel_id)
        norm_title = _norm(novel.title)
        if not norm_title:
            return {"healed": 0, "sources": [], "message": "Novel has no title to match on."}

        with ctx.db.session() as sess:
            others = sess.exec(sq.select(Novel).where(sq.col(Novel.id) != novel_id)).all()
        candidates = [n for n in others if _norm(n.title) == norm_title]
        if not candidates:
            return {
                "healed": 0,
                "sources": [],
                "message": "No other copy of this novel is in your library yet.",
            }

        missing = [c for c in ctx.chapters.list(novel_id=novel_id) if not c.is_available]
        if not missing:
            return {"healed": 0, "sources": [], "message": "Nothing missing — already complete."}
        missing_by_title: Dict[str, Any] = {}
        for c in missing:
            missing_by_title.setdefault(_norm(c.title), c)

        healed = 0
        filled_ids: set = set()
        # prefer the most complete other copy first
        for src in sorted(candidates, key=lambda n: -(n.chapter_count or 0)):
            for sc in ctx.chapters.list(novel_id=src.id):
                if not sc.is_available:
                    continue
                if ctx.images.list_ids(chapter_id=sc.id):
                    continue  # manga chapter — skip (per-source image ids)
                target = missing_by_title.get(_norm(sc.title))
                if not target or target.id in filled_ids:
                    continue
                try:
                    content = ctx.files.load_text(sc.content_file)
                    ctx.files.save_text(target.content_file, content)
                except Exception as e:
                    ctx.logger.debug(f"Heal copy failed for chapter {target.id}: {e}")
                    continue
                target.is_done = True
                with ctx.db.session() as sess:
                    sess.merge(target)
                    sess.commit()
                filled_ids.add(target.id)
                healed += 1
            if healed >= len(missing):
                break

        sources = sorted({c.domain for c in candidates})
        if not healed:
            return {
                "healed": 0,
                "sources": sources,
                "message": "Found other copies but no matching chapters to fill.",
            }
        return {
            "healed": healed,
            "sources": sources,
            "message": f"Filled {healed} missing chapter(s) from your other copies.",
        }
