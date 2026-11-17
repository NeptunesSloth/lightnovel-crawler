import errno
import sqlite3
import unittest

from lncrawl.services.scheduler.handlers.export_source import _disk_full


class DiskFullTests(unittest.TestCase):
    def test_a_full_disk_is_told_apart_from_other_failures(self) -> None:
        self.assertTrue(_disk_full(OSError(errno.ENOSPC, "No space left on device")))
        # SQLite says it in words, and not as an OSError
        self.assertTrue(_disk_full(sqlite3.OperationalError("database or disk is full")))
        # Windows' own wording, wrapped by something that lost the errno
        self.assertTrue(_disk_full(RuntimeError("There is not enough space on the disk")))
        self.assertFalse(_disk_full(OSError(errno.EACCES, "Permission denied")))
        self.assertFalse(_disk_full(ConnectionError("Connection reset by peer")))


if __name__ == "__main__":
    unittest.main()
