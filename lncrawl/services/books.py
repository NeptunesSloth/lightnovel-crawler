"""The books of a library kept on a drive that is not always plugged in.

A library too big for the laptop it is read on is split (see
`relocation.split`): the database, settings and every novel's cover stay in the
library on this computer, and the novels' folders (``novels/<id>``: chapters,
images, e-books) stay on the drive. The app then opens without the drive; what
is on it says so until the drive is back. Novels can be moved either way, a
few to read on the go or back to the drive to free room.

``book-drive.json`` in the library lists the novels whose folders are on the
drive. A novel not listed is on this computer. A new novel goes to the drive
when it is plugged in, unless asked otherwise (``new``).

`FileService.resolve` asks `route` where each of a novel's files is; nothing
else needs to know.
"""

import json
import logging
import os
from pathlib import Path
import shutil
import threading
from time import monotonic
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from ..context import ctx
from ..relocation import BOOK_STAMP, BOOKS, copy_tree, drive_roots, walk

logger = logging.getLogger(__name__)

# How long whether the drive is plugged in is taken as known.
PLUGGED_CHECK = 5.0
# How often book-drive.json is looked at for a change made by another process.
RELOAD_CHECK = 1.0
# Room kept free on the place novels are moved to.
MOVE_SPARE = 256 * 1024 * 1024
COVER = "cover.jpg"


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.relative_to(folder)
        return True
    except ValueError:
        return False


class BookDriveUnplugged(OSError):
    """A novel's books are on the drive, and it is not plugged in."""


class _StopMoving(OSError):
    """Moving novels cannot go on: no room, or the drive is gone."""


class BookDrive:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._loaded_from: Optional[Path] = None
        self._config: Dict[str, Any] = {}
        self._on_drive: Set[str] = set()
        # read here, with a copy left on the drive too: the drive keeps the
        # whole library (to hand on, say), this computer what is read on the go
        self._copies: Set[str] = set()
        self._here: Set[str] = set()  # novels found to be on this computer
        self._plugged = False
        self._checked = -PLUGGED_CHECK
        self._looked = -RELOAD_CHECK  # when book-drive.json was last looked at
        self._stamp: Optional[Tuple[int, int]] = None  # its time and size then
        self._moving: Dict[str, Any] = {"running": False, "done": 0, "total": 0, "failed": 0}

    # ------------------------------------------------------------------ #

    def _home(self) -> Path:
        return Path(ctx.config.app.app_dir)

    def _load(self) -> None:
        """Read ``book-drive.json`` from the library in use (lock held).

        Read again whenever the file has changed: a download worker is a
        process of its own with its own copy of this, and one that never looked
        again kept writing a novel to the drive after it had been brought here,
        then wrote its stale list over the new one.
        """
        home = self._home()
        file = home / BOOKS
        now = monotonic()
        if self._loaded_from == home and now - self._looked < RELOAD_CHECK:
            return
        self._looked = now
        try:
            info = file.stat()
            stamp: Optional[Tuple[int, int]] = (info.st_mtime_ns, info.st_size)
        except FileNotFoundError:
            stamp = None
        except OSError:
            return  # cannot be looked at just now: what is known stands
        if self._loaded_from == home and stamp == self._stamp:
            return
        if stamp is None:
            config: Dict[str, Any] = {}
        else:
            try:
                config = json.loads(file.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                # Not "there is no book drive": that sent every novel on the
                # drive to this computer, where none of its chapters are.
                logger.warning(f"Could not read {file} ({error}); trying again")
                self._looked = -RELOAD_CHECK
                return
            if not isinstance(config, dict) or not config.get("folder"):
                config = {}
        if self._loaded_from != home:
            self._checked = -PLUGGED_CHECK
        self._loaded_from = home
        self._stamp = stamp
        self._here.clear()
        self._config = config
        self._on_drive = {str(item) for item in config.get("novels") or []}
        self._copies = {str(item) for item in config.get("copies") or []}

    def _save(self) -> None:
        """Write ``book-drive.json`` back (lock held)."""
        self._config["novels"] = sorted(self._on_drive)
        self._config["copies"] = sorted(self._copies)
        target = self._home() / BOOKS
        writing = target.with_name(f"{BOOKS}.{os.getpid()}.part")
        writing.write_text(json.dumps(self._config, indent=2), encoding="utf-8")
        os.replace(writing, target)
        try:
            info = target.stat()
            self._stamp = (info.st_mtime_ns, info.st_size)
        except OSError:
            self._stamp = None

    @property
    def configured(self) -> bool:
        with self._lock:
            self._load()
            return bool(self._config)

    def folder(self) -> Optional[Path]:
        with self._lock:
            self._load()
            folder = self._config.get("folder")
            return Path(folder) if folder else None

    def _stamp_matches(self, folder: Path) -> bool:
        try:
            stamp = json.loads((folder / BOOK_STAMP).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return isinstance(stamp, dict) and stamp.get("id") == self._config.get("id")

    def _find_elsewhere(self, folder: Path) -> Optional[Path]:
        """The same folder under another drive letter: Windows gives a drive the next free one."""
        drive, tail = os.path.splitdrive(str(folder))
        if not drive:
            return None
        tail = tail.lstrip("\\/")
        for root in drive_roots():
            candidate = Path(root) / tail
            if candidate != folder and self._stamp_matches(candidate):
                return candidate
        return None

    def plugged(self) -> bool:
        """Whether the drive with the books is there now (checked every few seconds)."""
        with self._lock:
            self._load()
            if not self._config:
                return False
            if monotonic() - self._checked < PLUGGED_CHECK:
                return self._plugged
            folder = Path(self._config["folder"])
            plugged = self._stamp_matches(folder)
            if not plugged:
                found = self._find_elsewhere(folder)
                if found is not None:
                    logger.info(f"The book drive is at {found} now (was {folder})")
                    self._config["folder"] = str(found)
                    self._save()
                    plugged = True
            if plugged and not self._plugged and self._config.get("remove_later"):
                threading.Thread(target=self._remove_later, daemon=True).start()
            if plugged != self._plugged:
                self._here.clear()  # where an unlisted novel goes depends on the drive
            self._plugged = plugged
            self._checked = monotonic()
            return plugged

    # ------------------------------------------------------------------ #
    # where a novel's files are
    # ------------------------------------------------------------------ #

    def on_drive(self, novel_id: str) -> bool:
        with self._lock:
            self._load()
            return novel_id in self._on_drive

    def unavailable(self, novel_id: str) -> bool:
        """Whether this novel's books are on the drive and it is not plugged in."""
        return self.on_drive(novel_id) and not self.plugged()

    def require(self, novel_id: str) -> None:
        """Raise BookDriveUnplugged when this novel's books cannot be reached now.

        Asked before anything decides what is missing from a novel: with the
        drive unplugged every chapter looked missing, was marked not
        downloaded, fetched from the site again and then could not be saved.
        """
        if self.unavailable(novel_id):
            raise BookDriveUnplugged(
                f"This novel's books are on {self.folder()}, which is not plugged in"
            )

    def _place_new(self, novel_id: str) -> bool:
        """Whether a novel not in the list is on the drive (lock held).

        It is listed only once its folder is really there: looking a deleted
        novel's file up used to put its id back in the list. Until then a new
        novel goes where new novels go, decided afresh each time.
        """
        folder = self._home() / "novels" / novel_id
        try:
            here = any(entry.name != COVER for entry in os.scandir(folder))
        except OSError:
            here = False
        if here:
            self._here.add(novel_id)
            return False
        if not self.plugged():
            return False
        there = Path(self._config["folder"]) / "novels" / novel_id
        if there.is_dir():
            self._on_drive.add(novel_id)
            try:
                self._save()
            except OSError as error:
                logger.warning(f"Could not note that {novel_id} is on the book drive: {error}")
            return True
        return self._config.get("new") != "here"

    def route(self, relative: Path) -> Optional[Path]:
        """Where ``relative`` (a path in the library) is, when on the drive; else None."""
        parts = relative.parts
        if len(parts) < 2 or parts[0] != "novels":
            return None
        if len(parts) == 3 and parts[2] == COVER:
            return None  # covers stay here, to show with the drive unplugged
        novel_id = parts[1]
        with self._lock:
            self._load()
            if not self._config or novel_id in self._here:
                return None
            # looked at first: a drive back under another letter is found, and
            # the path below made with the letter it has now
            self.plugged()
            if novel_id not in self._on_drive and not self._place_new(novel_id):
                return None
            return Path(self._config["folder"]).joinpath(*parts)

    def folders(self, novel_id: str) -> List[Path]:
        """Every folder this novel has: here, and on the drive."""
        found = [self._home() / "novels" / novel_id]
        folder = self.folder()
        with self._lock:
            self._load()
            there = novel_id in self._on_drive or novel_id in self._copies
        if folder is not None and there:
            found.append(folder / "novels" / novel_id)
        return found

    def forget(self, novel_id: str) -> None:
        """The novel was deleted: its folder on the drive goes now, or once it is back."""
        with self._lock:
            self._load()
            if novel_id not in self._on_drive and novel_id not in self._copies:
                return
            self._on_drive.discard(novel_id)
            self._copies.discard(novel_id)
            if not self.plugged():
                later = set(self._config.get("remove_later") or [])
                later.add(novel_id)
                self._config["remove_later"] = sorted(later)
            self._save()

    def _remove_later(self) -> None:
        with self._lock:
            later = list(self._config.get("remove_later") or [])
            folder = Path(self._config["folder"])
        for novel_id in later:
            shutil.rmtree(folder / "novels" / novel_id, ignore_errors=True)
        with self._lock:
            left = set(self._config.get("remove_later") or []) - set(later)
            self._config["remove_later"] = sorted(left)
            self._save()

    # ------------------------------------------------------------------ #
    # settings and moving novels
    # ------------------------------------------------------------------ #

    def create(self, folder: Path) -> None:
        """Keep books in ``folder`` from now on (none are there yet: see `move`)."""
        import uuid

        home = self._home()
        if not folder.is_absolute():
            raise ValueError(f"{folder} is not a full path to a folder")
        if _inside(folder, home) or _inside(home, folder):
            raise ValueError(f"{folder} cannot be in the library, or the library in it")
        with self._lock:
            self._load()
            if self._config:
                raise ValueError(f"The books already have a drive: {self._config['folder']}")
            folder.mkdir(parents=True, exist_ok=True)
            ident = uuid.uuid4().hex
            (folder / BOOK_STAMP).write_text(
                json.dumps({"id": ident, "library": str(home)}, indent=2), encoding="utf-8"
            )
            self._config = {"folder": str(folder), "id": ident, "novels": [], "new": "drive"}
            self._on_drive = set()
            self._here.clear()
            self._checked = -PLUGGED_CHECK
            self._save()

    def set_new(self, where: str) -> None:
        if where not in ("drive", "here"):
            raise ValueError("New novels go to the drive or here")
        with self._lock:
            self._load()
            if not self._config:
                raise ValueError("There is no book drive")
            self._config["new"] = where
            self._here.clear()
            self._save()

    def status(self) -> Dict[str, Any]:
        with self._lock:
            self._load()
            if not self._config:
                return {"configured": False, "moving": dict(self._moving)}
            plugged = self.plugged()
            folder = Path(self._config["folder"])
            free: Optional[int] = None
            if plugged:
                try:
                    free = shutil.disk_usage(folder).free
                except OSError:
                    pass
            return {
                "configured": True,
                "folder": str(folder),
                "plugged": plugged,
                "free": free,
                "new": self._config.get("new") or "drive",
                "on_drive": sorted(self._on_drive),
                "copies": sorted(self._copies),
                "moving": dict(self._moving),
            }

    def move(
        self, novel_ids: Iterable[str], to_drive: bool, keep_copy: bool = True
    ) -> Dict[str, Any]:
        """Move these novels' books to the drive (``to_drive``) or here, in the background.

        Brought here with ``keep_copy``, the drive keeps its copy as well, so it
        still holds the whole library; sent back, the copy here is what is new.
        """
        with self._lock:
            self._load()
            if not self._config:
                raise ValueError("There is no book drive")
            if not self.plugged():
                raise ValueError(f"The book drive ({self._config['folder']}) is not plugged in")
            if self._moving["running"]:
                raise ValueError("Novels are being moved already")
            # to the drive: whatever is read here (a copy there or not);
            # here: whatever is read from the drive
            todo = [
                novel_id
                for novel_id in dict.fromkeys(novel_ids)
                if (novel_id in self._on_drive) != to_drive
            ]
            self._moving = {
                "running": True,
                "to": "drive" if to_drive else "here",
                "done": 0,
                "total": len(todo),
                "failed": 0,
                "error": "",
            }
        threading.Thread(
            target=self._move, args=(todo, to_drive, keep_copy), daemon=True, name="book-move"
        ).start()
        return self.status()

    def _move(self, todo: List[str], to_drive: bool, keep_copy: bool) -> None:
        try:
            for novel_id in todo:
                try:
                    self._move_one(novel_id, to_drive, keep_copy)
                except Exception as error:
                    logger.warning(f"Could not move novel {novel_id}: {error}")
                    with self._lock:
                        self._moving["failed"] += 1
                        self._moving["error"] = self._moving["error"] or str(error)
                    if isinstance(error, _StopMoving):
                        # no room, or the drive is gone: the rest would fail the same way
                        with self._lock:
                            self._moving["failed"] += (
                                self._moving["total"] - self._moving["done"] - 1
                            )
                            self._moving["done"] = self._moving["total"]
                        return
                with self._lock:
                    self._moving["done"] += 1
        finally:
            with self._lock:
                self._moving["running"] = False

    @staticmethod
    def _listing(folder: Path) -> Dict[str, int]:
        """Every file under ``folder`` with its size."""
        found: Dict[str, int] = {}
        for relative, entry in walk(folder):
            try:
                found[relative] = entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
        return found

    def _move_one(self, novel_id: str, to_drive: bool, keep_copy: bool = True) -> None:
        """Copy a novel's books across, switch to the copy, then remove what was copied.

        Nothing is removed that is not seen, whole, in the other place: a drive
        that dropped out for a moment used to count as "nothing to copy", after
        which the novel was switched over and its only copy deleted.
        """
        home = self._home() / "novels" / novel_id
        with self._lock:
            root = Path(self._config["folder"])
        drive = root / "novels" / novel_id
        source, target = (home, drive) if to_drive else (drive, home)
        if not self.plugged() or not root.is_dir():
            raise _StopMoving(f"The book drive ({root}) is not plugged in")
        if not source.is_dir():
            if to_drive:
                return  # nothing of it here: it is on the drive already, or has no books
            raise OSError(f"{source} cannot be found on the drive")
        listed = self._listing(source)
        try:
            free = shutil.disk_usage(target.parent if target.parent.is_dir() else root).free
        except OSError:
            free = None
        already = self._listing(target) if target.is_dir() else {}
        need = sum(size for name, size in listed.items() if already.get(name) != size)
        if free is not None and free < need + MOVE_SPARE:
            raise _StopMoving(
                f"Not enough room in {target.parent}: {need // (1024 * 1024):,} MB more is needed"
            )
        copy_tree(source, target, (), lambda files, size: None)
        arrived = self._listing(target)
        missing = [name for name, size in listed.items() if arrived.get(name) != size]
        if missing:
            raise OSError(f"{len(missing)} file(s) did not arrive whole (first: {missing[0]})")
        with self._lock:
            if to_drive:
                self._on_drive.add(novel_id)
                self._copies.discard(novel_id)
            else:
                self._on_drive.discard(novel_id)
                if keep_copy:
                    self._copies.add(novel_id)
            self._here.discard(novel_id)
            self._save()
        if not to_drive and keep_copy:
            return  # the drive keeps its copy
        # The copy is in use from here. What was written to the old place while
        # it was being copied comes across too, then only what is seen in the
        # new place, the same size, goes from the old one (a cover stays here).
        try:
            copy_tree(source, target, (), lambda files, size: None)
        except OSError as error:
            logger.warning(f"Could not bring the last files of {novel_id} across: {error}")
        arrived = self._listing(target)
        for name, size in self._listing(source).items():
            if to_drive and name == COVER:
                continue
            if arrived.get(name) != size:
                continue
            try:
                os.unlink(source / name)
            except OSError:
                pass
        for folder, _, _ in sorted(os.walk(source), reverse=True):
            if to_drive and Path(folder) == source:
                continue
            try:
                os.rmdir(folder)  # only when empty
            except OSError:
                pass
