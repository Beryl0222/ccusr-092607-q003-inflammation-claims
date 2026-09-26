import threading
import unittest

from inflammation_claims.errors import QuotaExhausted
from inflammation_claims.quota import QueryQuota


class QueryQuotaTests(unittest.TestCase):
    def test_reserve_and_remaining(self):
        quota = QueryQuota(100)
        quota.reserve("a", 30)
        self.assertEqual(70, quota.remaining)

    def test_reservation_is_idempotent(self):
        quota = QueryQuota(100)
        self.assertEqual(30, quota.reserve("a", 30))
        self.assertEqual(30, quota.reserve("a", 999))
        self.assertEqual(70, quota.remaining)

    def test_over_budget_is_atomic(self):
        quota = QueryQuota(10)
        with self.assertRaises(QuotaExhausted):
            quota.reserve("a", 11)
        self.assertEqual(10, quota.remaining)

    def test_release_returns_units(self):
        quota = QueryQuota(10)
        quota.reserve("a", 4)
        self.assertEqual(4, quota.release("a"))
        self.assertEqual(10, quota.remaining)
        self.assertEqual(0, quota.release("a"))

    def test_concurrent_reservations_never_oversell(self):
        quota = QueryQuota(500)
        winners: list[int] = []
        winners_lock = threading.Lock()

        def worker(i):
            try:
                quota.reserve(f"res-{i}", 1)
                with winners_lock:
                    winners.append(i)
            except QuotaExhausted:
                pass

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(1000)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(500, len(winners))
        self.assertEqual(0, quota.remaining)


if __name__ == "__main__":
    unittest.main()
