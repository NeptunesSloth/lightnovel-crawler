from threading import Event, Thread
import unittest

from lncrawl.exceptions import AbortedException
from lncrawl.utils.fts_store import FTSStore


class SlowItems(dict):
    """A mapping whose keys take a while to hand over: an insert caught half way."""

    def __init__(self, entered: Event, release: Event) -> None:
        super().__init__(key="value")
        self.entered, self.release = entered, release

    def keys(self):
        self.entered.set()
        self.release.wait(5)
        return super().keys()


class CloseTests(unittest.TestCase):
    def test_close_waits_for_an_insert_that_is_under_way(self) -> None:
        # The app closed while the source list was still loading: the connection
        # was closed under the thread writing to it, and the process died with
        # a segmentation fault (about one exit in eight, in a test of a quick run).
        store = FTSStore()
        entered, release = Event(), Event()
        failures = []

        def insert() -> None:
            try:
                store.insert_dict(SlowItems(entered, release))
            except Exception as error:
                failures.append(error)

        writer = Thread(target=insert)
        writer.start()
        self.assertTrue(entered.wait(5))
        closer = Thread(target=store.close)
        closer.start()
        closer.join(0.3)
        self.assertTrue(closer.is_alive(), "closed while an insert was using the connection")
        release.set()
        writer.join(5)
        closer.join(5)
        self.assertFalse(closer.is_alive())
        self.assertEqual(failures, [])  # the insert finished on an open connection

    def test_a_closed_store_refuses_work_instead_of_touching_the_connection(self) -> None:
        store = FTSStore()
        store.insert("key", "value")
        store.close()
        with self.assertRaises(AbortedException):
            store.count()


if __name__ == "__main__":
    unittest.main()
