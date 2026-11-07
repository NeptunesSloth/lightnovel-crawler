"""Each service on the app context is built once, even when threads race for it."""

from threading import Barrier, Thread
import time
import unittest

from lncrawl.context import cached_property


class Context:
    built = 0

    @cached_property
    def service(self) -> object:
        Context.built += 1
        time.sleep(0.05)  # long enough for every thread to be waiting on it
        return object()


class ContextServiceTests(unittest.TestCase):
    def test_racing_threads_get_one_service(self) -> None:
        Context.built = 0
        context = Context()
        start = Barrier(8)
        seen = []

        def ask() -> None:
            start.wait()
            seen.append(context.service)

        threads = [Thread(target=ask) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(Context.built, 1)
        self.assertEqual(len({id(service) for service in seen}), 1)

    def test_behaves_like_a_cached_property(self) -> None:
        context = Context()
        first = context.service
        self.assertIs(context.service, first)
        self.assertIn("service", context.__dict__)  # what ctx.destroy() checks
        del context.__dict__["service"]
        self.assertIsNot(context.service, first)


if __name__ == "__main__":
    unittest.main()
