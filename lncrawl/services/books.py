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
from typing import Any, Dict, Iterable, List, Optional, Set

from ..context import ctx
from ..relocation import BOOK_STAMP, BOOKS, copy_tree, drive_roots

logger = logging.getLogger(__name__)

# How long whether the drive is plugged in is taken as known.
PLUGGED_CHECK = 5.0
COVER = "cover.jpg"


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.relative_to(folder)
        return True
    except ValueError:
        return False


class BookDriveUnplugged(OSError):
    """A novel's books are on the drive, and it is not plugged in."""


class BookDrive:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._loaded_from: Optional[Path] = None
        self._config: Dict[str, Any] = {}
        self._on_drive: Set[str] = set()
        self._here: Set[str] = set()  # novels found to be on this computer
        self._plugged = False
        self._checked = -PLUGGED_CHECK
        self._moving: Dict[str, Any] = {"running": False, "done": 0, "total": 0, "failed": 0}

    # ------------------------------------------------------------------ #

    def _home(self) -> Path:
        return Path(ctx.config.app.app_dir)

    def _load(self) -> None:
        """Read ``book-drive.json`` from the library in use (lock held)."""
        home = self._home()
        if self._loaded_from == home:
            return
        self._loaded_from = home
        self._here.clear()
        self._checked = -PLUGGED_CHECK
        try:
            config = json.loads((home / BOOKS).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            config = {}
        if not isinstance(config, dict) or not config.get("folder"):
            config = {}
        self._config = config
        self._on_drive = {str(item) for item in config.get("novels") or []}

    def _save(self) -> None:
        """Write ``book-drive.json`` back (lock held)."""
        self._config["novels"] = sorted(self._on_drive)
        target = self._home() / BOOKS
        writing = target.with_name(f"{BOOKS}.part")
        writing.write_text(json.dumps(self._config, indent=2), encoding="utf-8")
        os.replace(writing, target)

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

    def _place_new(self, novel_id: str) -> bool:
        """Whether a novel not placed yet goes on the drive (lock held)."""
        folder = self._home() / "novels" / novel_id
        try:
            here = any(entry.name != COVER for entry in os.scandir(folder))
        except OSError:
            here = False
        if here or self._config.get("new") == "here" or not self.plugged():
            self._here.add(novel_id)
            return False
        self._on_drive.add(novel_id)
        try:
            self._save()
        except OSError as error:
            logger.warning(f"Could not note that {novel_id} is on the book drive: {error}")
        return True

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
            if novel_id not in self._on_drive and not self._place_new(novel_id):
                return None
            return Path(self._config["folder"]).joinpath(*parts)

    def folders(self, novel_id: str) -> List[Path]:
        """Every folder this novel has: here, and on the drive."""
        found = [self._home() / "novels" / novel_id]
        folder = self.folder()
        if folder is not None and self.on_drive(novel_id):
            found.append(folder / "novels" / novel_id)
        return found

    def forget(self, novel_id: str) -> None:
        """The novel was deleted: its folder on the drive goes now, or once it is back."""
        with self._lock:
            self._load()
            if novel_id not in self._on_drive:
                return
            self._on_drive.discard(novel_id)
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
                "moving": dict(self._moving),
            }

    def move(self, novel_ids: Iterable[str], to_drive: bool) -> Dict[str, Any]:
        """Move these novels' books to the drive (``to_drive``) or here, in the background."""
        with self._lock:
            self._load()
            if not self._config:
                raise ValueError("There is no book drive")
            if not self.plugged():
                raise ValueError(f"The book drive ({self._config['folder']}) is not plugged in")
            if self._moving["running"]:
                raise ValueError("Novels are being moved already")
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
            target=self._move, args=(todo, to_drive), daemon=True, name="book-move"
        ).start()
        return self.status()

    def _move(self, todo: List[str], to_drive: bool) -> None:
        try:
            for novel_id in todo:
                try:
                    self._move_one(novel_id, to_drive)
                except Exception as error:
                    logger.warning(f"Could not move novel {novel_id}: {error}")
                    with self._lock:
                        self._moving["failed"] += 1
                        self._moving["error"] = self._moving["error"] or str(error)
                with self._lock:
                    self._moving["done"] += 1
        finally:
            with self._lock:
                self._moving["running"] = False

    def _move_one(self, novel_id: str, to_drive: bool) -> None:
        home = self._home() / "novels" / novel_id
        with self._lock:
            drive = Path(self._config["folder"]) / "novels" / novel_id
        source, target = (home, drive) if to_drive else (drive, home)
        if source.is_dir():
            copy_tree(source, target, (), lambda files, size: None)
        with self._lock:
            if to_drive:
                self._on_drive.add(novel_id)
            else:
                self._on_drive.discard(novel_id)
            self._here.discard(novel_id)
            self._save()
        # the copy is in use from here; the old one goes (a cover stays here)
        if not source.is_dir():
            return
        for entry in os.scandir(source):
            if to_drive and entry.name == COVER:
                continue
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path, ignore_errors=True)
            else:
                try:
                    os.unlink(entry.path)
                except OSError:
                    pass
        if not to_drive:
            try:
                source.rmdir()
            except OSError:
                pass
