"""Build an offline EPUB library, including books retained only in export ZIPs."""

import hashlib
from pathlib import Path
import shutil
from tempfile import TemporaryFile
import zipfile

from .scheduler.handlers.export_source import _build_index_html


def build_library_zip(data_dir: Path, output: Path) -> int:
    entries = []
    seen = set()
    names = set()

    with zipfile.ZipFile(output, "w", zipfile.ZIP_STORED, allowZip64=True) as target:

        def add(source, name):
            # Spool to disk: even very large books never need to fit in memory.
            digest = hashlib.sha256()
            with TemporaryFile() as spool:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    spool.write(chunk)
                key = digest.digest()
                if key in seen:
                    return
                spool.seek(0)
                if not zipfile.is_zipfile(spool):
                    return
                spool.seek(0)
                stem = Path(name).stem
                arcname = f"books/{stem}.epub"
                counter = 2
                while arcname.casefold() in names:
                    arcname = f"books/{stem} ({counter}).epub"
                    counter += 1
                with target.open(arcname, "w", force_zip64=True) as dest:
                    shutil.copyfileobj(spool, dest)
                names.add(arcname.casefold())
                seen.add(key)
                entries.append((arcname, stem, "unknown"))

        for file in sorted((data_dir / "novels").rglob("*.epub")):
            with file.open("rb") as source:
                add(source, file.name)
        # Bulk exports can outlive their temporary ebook files. Include those too.
        for file in sorted((data_dir / "exports").glob("*.zip")):
            if file.resolve() == output.resolve() or file.name.startswith("backup-"):
                continue
            try:
                archive = zipfile.ZipFile(file)
            except zipfile.BadZipFile:
                continue
            with archive:
                for member in archive.infolist():
                    if not member.is_dir() and member.filename.lower().endswith(".epub"):
                        with archive.open(member) as source:
                            add(source, member.filename.replace("\\", "/").rsplit("/", 1)[-1])
        index = _build_index_html("Saved EPUB library", entries)
        index = index.replace("(incomplete)", "(completeness not checked)")
        target.writestr("index.html", index)
        target.writestr(
            "README.txt",
            "Extract this ZIP, then open index.html to browse your EPUBs.\n"
            "Identical EPUB files are included once. Different versions are kept.\n"
            "This export does not download or repair chapters.\n",
        )
    return len(entries)
