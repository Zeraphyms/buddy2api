import time
import unittest

from fastapi import HTTPException

from admin.quota import DailyQuota


class FakeStore:
    def __init__(self):
        self.data = {}
        self.saves = 0

    def save(self):
        self.saves += 1


class DailyQuotaTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.now = [1_760_000_000.0]
        self.quota = DailyQuota(self.store, 1000, clock=lambda: self.now[0])

    def test_allows_up_to_limit_then_rejects(self):
        for _ in range(1000):
            self.quota.check_and_count("k")
        self.assertEqual(self.quota.used("k"), 1000)
        with self.assertRaises(HTTPException) as ctx:
            self.quota.check_and_count("k")
        self.assertEqual(ctx.exception.status_code, 429)
        # 被拒绝的请求不计数
        self.assertEqual(self.quota.used("k"), 1000)

    def test_keys_are_counted_separately(self):
        self.quota.check_and_count("a")
        self.quota.check_and_count("a")
        self.quota.check_and_count("b")
        self.assertEqual(self.quota.used("a"), 2)
        self.assertEqual(self.quota.used("b"), 1)

    def test_resets_on_next_day(self):
        for _ in range(5):
            self.quota.check_and_count("k")
        self.now[0] += 86400
        self.assertEqual(self.quota.used("k"), 0)
        self.quota.check_and_count("k")
        self.assertEqual(self.quota.used("k"), 1)
        self.assertEqual(len(self.store.data["quota_daily"]), 1)

    def test_zero_limit_disables_quota(self):
        q = DailyQuota(self.store, 0, clock=lambda: self.now[0])
        for _ in range(50):
            q.check_and_count("k")
        self.assertEqual(q.used("k"), 0)


if __name__ == "__main__":
    unittest.main()
