import logging
import re
import shutil
from typing import Any, Dict, Iterable, List, Optional, Tuple

import sqlmodel as sq

from ..context import ctx
from ..dao import (
    Artifact,
    Chapter,
    ChapterImage,
    ChapterTranslation,
    LanguageCode,
    Library,
    LibraryNovel,
    Novel,
    NovelTranslation,
    ReadHistory,
    User,
    UserRole,
    Volume,
    VolumeTranslation,
)
from ..exceptions import ServerErrors
from ..server.models import Paginated
from ..utils.url_tools import extract_host

logger = logging.getLogger(__name__)

# Everything that belongs to one novel, children before parents.
_NOVEL_ROWS: Tuple[Any, ...] = (
    ChapterImage,
    ChapterTranslation,
    Chapter,
    VolumeTranslation,
    Volume,
    Artifact,
    NovelTranslation,
    ReadHistory,
    LibraryNovel,
)


_RE_PAGE = re.compile(r"images/([0-9a-fA-F]{32})[.]jpg")


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
                # Match the indexed `domain` column instead of scanning every
                # row's url for a substring. Whatever the caller passed is put
                # through the same normaliser that filled the column, so a bare
                # host, a "www." prefix and a whole novel URL all still work —
                # what no longer matches is an arbitrary fragment of a URL.
                conditions.append(Novel.domain == extract_host(domain.strip()))

            if search:
                pattern = f"%{search}%"
                conditions.append(
                    sq.or_(
                        sq.col(Novel.title).ilike(pattern),
                        sq.col(Novel.authors).ilike(pattern),
                        sq.col(Novel.synopsis).ilike(pattern),
                    )
                )

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

    def titles_by_id(self, novel_ids: List[str]) -> Dict[str, str]:
        """Titles for the given novel ids, in one query."""
        if not novel_ids:
            return {}
        with ctx.db.session() as sess:
            rows = sess.exec(
                sq.select(Novel.id, Novel.title).where(sq.col(Novel.id).in_(novel_ids))
            ).all()
            return {nid: title for nid, title in rows}

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

    def can_delete(self, novel_id: str, user: User) -> bool:
        """Whether `user` may remove this novel from the library.

        The library is shared, so only an admin may delete just any novel. Anyone
        else may delete the ones that are theirs alone: every file kept for it is
        one they made or imported, and nobody else has it on a shelf or has read
        it.
        """
        if user.role == UserRole.ADMIN:
            return True
        with ctx.db.session() as sess:
            owners = set(
                sess.exec(sq.select(Artifact.user_id).where(Artifact.novel_id == novel_id)).all()
            )
            if owners != {user.id}:
                return False
            shelved = sess.exec(
                sq.select(LibraryNovel.novel_id)
                .join(Library, sq.col(Library.id) == LibraryNovel.library_id)
                .where(LibraryNovel.novel_id == novel_id)
                .where(Library.user_id != user.id)
                .limit(1)
            ).first()
            if shelved:
                return False
            read = sess.exec(
                sq.select(ReadHistory.id)
                .where(ReadHistory.novel_id == novel_id)
                .where(ReadHistory.user_id != user.id)
                .limit(1)
            ).first()
            return read is None

    def delete(self, novel_id: str) -> bool:
        books = getattr(ctx, "books", None)  # a novel's books may be on a drive too
        folders = books.folders(novel_id) if books is not None else []
        for novel_dir in folders or [ctx.files.resolve(f"novels/{novel_id}")]:
            shutil.rmtree(novel_dir, True)
        if books is not None:
            books.forget(novel_id)
        with ctx.db.session() as sess:
            novel = sess.get(Novel, novel_id)
            novel_title = novel.title if novel else ""
            # SQLite is not told to cascade, so what belongs to the novel is
            # removed by hand: its page rows left behind made a later import of
            # the same manga show no pages at all
            for table in _NOVEL_ROWS:
                sess.exec(sq.delete(table).where(sq.col(table.novel_id) == novel_id))
            if novel:
                sess.delete(novel)
            sess.commit()
        if not novel:
            return True
        ctx.recommendations.invalidate(novel_id)
        ctx.recommendations.index_remove(novel_id, novel_title)
        return True

    def remove_orphans(self) -> int:
        """Remove rows left behind by novels deleted before deleting cleaned up. How many.

        Run when the app starts. Files are not touched: a deleted novel's folder
        went with it.
        """
        removed = 0
        with ctx.db.session() as sess:
            novels = sq.select(Novel.id)
            for table in _NOVEL_ROWS:
                result = sess.exec(sq.delete(table).where(sq.col(table.novel_id).not_in(novels)))
                removed += getattr(result, "rowcount", 0) or 0
            sess.commit()
        if removed:
            logger.info(f"Removed {removed} row(s) left behind by deleted novels")
        return removed

    def _take_over(self, kept_id: str, gone_id: str) -> None:
        """Before a copy is deleted, give the kept one what it uses of it.

        An import of a manga already in the library files its pages under the
        copy that had them first, so the kept copy's chapters can show pages
        stored with the copy about to go: deleting it took them, and the kept
        manga showed "(image unavailable)". Those pages move to the kept copy.
        The shelves the copy was on get the kept one instead.
        """
        with ctx.db.session() as sess:
            theirs = sess.exec(
                sq.select(ChapterImage).where(ChapterImage.novel_id == gone_id)
            ).all()
            if theirs:
                ids = {image.id for image in theirs}
                used: Dict[str, str] = {}  # image id -> the kept chapter showing it
                for chapter in sess.exec(sq.select(Chapter).where(Chapter.novel_id == kept_id)):
                    try:
                        text = ctx.files.load_text(chapter.content_file)
                    except Exception:
                        continue
                    for found in _RE_PAGE.findall(text):
                        found = found.lower()
                        if found in ids:
                            used.setdefault(found, chapter.id)
                for image in theirs:
                    if image.id not in used:
                        continue
                    source = ctx.files.resolve(f"novels/{gone_id}/images/{image.id}.jpg")
                    target = ctx.files.resolve(f"novels/{kept_id}/images/{image.id}.jpg")
                    try:
                        if source.is_file() and not target.is_file():
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(source, target)
                    except OSError as error:
                        logger.warning(f"Could not keep page {image.id}: {error}")
                        continue
                    image.novel_id = kept_id
                    image.chapter_id = used[image.id]
                    sess.add(image)
            shelves = set(
                sess.exec(
                    sq.select(LibraryNovel.library_id).where(LibraryNovel.novel_id == kept_id)
                ).all()
            )
            for row in sess.exec(
                sq.select(LibraryNovel).where(LibraryNovel.novel_id == gone_id)
            ).all():
                if row.library_id not in shelves:
                    sess.add(LibraryNovel(library_id=row.library_id, novel_id=kept_id))
                    shelves.add(row.library_id)
            sess.commit()

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

        # one listing of each folder, not a look at every chapter file
        rows = ctx.chapters.list(novel_id=novel_id)
        have = ctx.files.chapters_with_content(novel_id, [c.serial for c in rows])
        missing = [c for c in rows if c.serial not in have]
        if not missing:
            return {"healed": 0, "sources": [], "message": "Nothing missing — already complete."}
        missing_by_title: Dict[str, Any] = {}
        for c in missing:
            missing_by_title.setdefault(_norm(c.title), c)

        healed = 0
        filled_ids: set = set()
        filled: List[Any] = []
        # prefer the most complete other copy first
        for src in sorted(candidates, key=lambda n: -(n.chapter_count or 0)):
            source_rows = ctx.chapters.list(novel_id=src.id)
            source_have = ctx.files.chapters_with_content(src.id, [sc.serial for sc in source_rows])
            with ctx.db.session() as sess:
                with_images = set(
                    sess.exec(
                        sq.select(ChapterImage.chapter_id)
                        .where(ChapterImage.novel_id == src.id)
                        .distinct()
                    ).all()
                )
            for sc in source_rows:
                if sc.serial not in source_have:
                    continue
                if sc.id in with_images:
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
                filled.append(target)
                filled_ids.add(target.id)
                healed += 1
            if healed >= len(missing):
                break

        # One transaction for the whole fill. A session and a commit per chapter
        # meant a separate fsync each, which is most of the wall time when a few
        # hundred gaps are filled at once — and left a half-healed novel behind
        # if the run died midway.
        if filled:
            with ctx.db.session() as sess:
                for target in filled:
                    sess.merge(target)
                sess.commit()

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

    def merge_duplicates(
        self, novel_id: str, among: Optional[Iterable[str]] = None
    ) -> Dict[str, Any]:
        """Merge all same-title copies of a novel into the most complete one.

        The copy with the most chapters becomes canonical; matching chapters from
        the other copies are healed into it, then the other copies are deleted.
        Chapters that exist only in a deleted copy under a different chapter title
        cannot be matched and are lost — the UI must warn before calling this.
        Returns the canonical novel id and a summary.
        """
        novel = self.get(novel_id)
        norm_title = _norm(novel.title)
        if not norm_title:
            return {"canonical_id": novel_id, "merged": 0, "message": "Novel has no title."}

        with ctx.db.session() as sess:
            all_novels = sess.exec(sq.select(Novel)).all()
        group = [n for n in all_novels if _norm(n.title) == norm_title]
        if among is not None:
            chosen = set(among)  # only these copies (the clean-up found them to be one story)
            group = [n for n in group if n.id in chosen]
        if len(group) < 2:
            return {"canonical_id": novel_id, "merged": 0, "message": "No other copy found."}

        # A title with no Latin letters or digits in common says nothing: two
        # different novels titled in Chinese ending in "2" both came down to "2".
        if not re.search(r"[a-z]", norm_title):
            group = [n for n in group if (n.title or "").strip() == (novel.title or "").strip()]
            if len(group) < 2:
                return {"canonical_id": novel_id, "merged": 0, "message": "No other copy found."}
        books = getattr(ctx, "books", None)
        if books is not None:
            for copy in group:
                books.require(copy.id)  # nothing is merged, or deleted, blind

        def downloaded(copy: Novel) -> Dict[str, int]:
            """A copy's downloaded chapters, by title as matched across copies."""
            rows = ctx.chapters.list(novel_id=copy.id)
            have = ctx.files.chapters_with_content(copy.id, [row.serial for row in rows])
            return {_norm(row.title): row.serial for row in rows if row.serial in have}

        # The copy with the most chapters downloaded is kept, not the one with
        # the longest list: a copy listing 500 and holding none used to win
        # over one holding all 480 of its own.
        held = {copy.id: downloaded(copy) for copy in group}
        canonical = max(group, key=lambda n: (len(held[n.id]), n.chapter_count or 0))
        heal = self.heal_from_library(canonical.id)
        kept = downloaded(canonical)
        merged = 0
        left: List[str] = []
        for other in group:
            if other.id == canonical.id:
                continue
            # A copy goes only when everything downloaded in it is now in the
            # one kept. Manga pages are never carried over, and a copy whose
            # chapters could not be read was deleted all the same.
            only_there = [title for title in held[other.id] if title not in kept]
            if only_there:
                left.append(f"{other.title} ({len(only_there)} chapter(s) only it has)")
                continue
            self._take_over(canonical.id, other.id)
            self.delete(other.id)
            merged += 1
        healed = heal.get("healed", 0)
        message = (
            f"Merged {merged} duplicate cop{'ies' if merged != 1 else 'y'} into the most "
            f"complete one ({healed} chapter(s) filled in)."
        )
        if left:
            message += " Kept, since it would have lost chapters: " + "; ".join(left) + "."
        return {
            "canonical_id": canonical.id,
            "merged": merged,
            "healed": healed,
            "kept": len(left),
            "message": message,
        }
