"""Looking through the folders on the computer the app runs on, to pick one.

For the folder boxes in Tools (where to move the library, where to save a
ZIP): typing a path from memory is where people got stuck. Only folders are
listed, never files, and only on the app's own computer, for its admin.
"""

import os
from pathlib import Path
import re
import shutil
from typing import Any, Dict, List, Optional

from ..relocation import drive_roots, is_library

# More than this in one folder are not listed; the rest are counted.
MOST_FOLDERS = 2000
_HIDDEN = 0x2 | 0x4  # Windows: hidden, system


def _as_folder(path: str) -> Path:
    path = path.strip()
    if re.fullmatch(r"[A-Za-z]:", path):  # "F:" is the drive, not the folder last used on it
        path += os.sep
    folder = Path(path).expanduser()
    if not folder.is_absolute():
        raise ValueError(f"{path} is not a full path to a folder")
    return folder


def _hidden(entry: "os.DirEntry[str]") -> bool:
    if entry.name.startswith("."):
        return True
    try:
        attributes = getattr(entry.stat(), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attributes & _HIDDEN)


def _space(folder: Path) -> Dict[str, Optional[int]]:
    try:
        usage = shutil.disk_usage(folder)
    except OSError:
        return {"free": None, "total": None}
    return {"free": usage.free, "total": usage.total}


def places() -> List[Dict[str, Any]]:
    """Where to start looking: every drive, and the user's own folders."""
    found: List[Dict[str, Any]] = []
    roots = drive_roots()
    if os.name != "nt":
        roots = [Path("/"), *roots]
    for root in roots:
        found.append({"name": str(root), "path": str(root), "kind": "drive", **_space(root)})
    home = Path.home()
    for name, folder in (
        ("Home", home),
        ("Desktop", home / "Desktop"),
        ("Documents", home / "Documents"),
    ):
        if folder.is_dir():
            found.append({"name": name, "path": str(folder), "kind": "folder"})
    return found


def list_folder(path: Optional[str]) -> Dict[str, Any]:
    """The folders in ``path``, or the places to start from when it is empty.

    ValueError says why a path cannot be listed.
    """
    if not path or not path.strip():
        return {"path": None, "parent": None, "folders": [], "more": 0, "places": places()}
    folder = _as_folder(path)
    if not folder.is_dir():
        raise ValueError(f"{folder} cannot be found")
    try:
        entries = list(os.scandir(folder))
    except PermissionError as error:
        raise ValueError(f"{folder} cannot be opened: permission denied") from error
    except OSError as error:
        raise ValueError(f"{folder} cannot be opened: {error.strerror or error}") from error
    folders: List[Dict[str, Any]] = []
    for entry in sorted(entries, key=lambda item: item.name.casefold()):
        try:
            if not entry.is_dir() or _hidden(entry):
                continue
        except OSError:
            continue
        folders.append({"name": entry.name, "path": entry.path})
    more = max(0, len(folders) - MOST_FOLDERS)
    folders = folders[:MOST_FOLDERS]
    for item in folders:
        item["library"] = is_library(Path(item["path"]))
    parent = folder.parent if folder.parent != folder else None
    return {
        "path": str(folder),
        "parent": str(parent) if parent else None,
        "library": is_library(folder),
        "folders": folders,
        "more": more,
        "places": None,
        **_space(folder),
    }


def new_folder(path: str, name: str) -> str:
    """Make a folder called ``name`` in ``path``; its full path. ValueError says why not."""
    name = name.strip()
    if not name or name in (".", "..") or re.search(r'[\\/:*?"<>|\x00-\x1f]', name):
        raise ValueError(f'"{name}" cannot be a folder name')
    parent = _as_folder(path)
    if not parent.is_dir():
        raise ValueError(f"{parent} cannot be found")
    folder = parent / name
    if folder.exists():
        raise ValueError(f"{folder} already exists")
    try:
        folder.mkdir()
    except OSError as error:
        raise ValueError(f"{folder} could not be made: {error.strerror or error}") from error
    return str(folder)
