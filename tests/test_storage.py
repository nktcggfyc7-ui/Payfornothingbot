import unittest
from pathlib import Path
import uuid

from main import Plan, Storage, utc_now


class StorageFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_root = Path.cwd() / "data" / "test-artifacts"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.db_path = temp_root / f"test-{uuid.uuid4().hex}.db"
        self.storage = Storage(self.db_path)
        self.addCleanup(self.cleanup_storage)
        self.user = {
            "id": 101,
            "username": "tester",
            "first_name": "Test",
            "last_name": "User",
            "language_code": "ru",
        }
        self.referrer = {
            "id": 202,
            "username": "legend",
            "first_name": "Legend",
            "last_name": "User",
            "language_code": "ru",
        }
        self.plan_small = Plan(
            code="basic_void",
            title="Обычное nothing",
            price_rub=100,
            tagline="tag",
            confirmation_text="text",
        )
        self.plan_large = Plan(
            code="legend_void",
            title="Легендарное nothing",
            price_rub=1000,
            tagline="tag",
            confirmation_text="text",
        )

    def cleanup_storage(self) -> None:
        self.storage.close()
        if self.db_path.exists():
            self.db_path.unlink()

    def test_referral_link_is_saved_once(self) -> None:
        self.storage.upsert_user(self.referrer)
        self.storage.upsert_user(self.user)
        linked = self.storage.link_referrer(101, 202)
        linked_again = self.storage.link_referrer(101, 202)

        row = self.storage.get_user(101)
        referrer = self.storage.get_user(202)

        self.assertTrue(linked)
        self.assertFalse(linked_again)
        self.assertEqual(row["referred_by_user_id"], 202)
        self.assertEqual(referrer["referral_count"], 1)

    def test_payment_approval_updates_user_stats(self) -> None:
        self.storage.upsert_user(self.user)
        payment = self.storage.create_or_update_pending_payment(self.user, self.plan_small, "2200")
        submitted = self.storage.submit_latest_payment(101, proof_text="paid", proof_file_id=None, proof_kind="text")
        approved, changed = self.storage.approve_payment(int(submitted["id"]), 999)

        row = self.storage.get_user(101)

        self.assertIsNotNone(payment)
        self.assertTrue(changed)
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(row["approved_total_amount"], 100)
        self.assertEqual(row["approved_payments_count"], 1)
        self.assertEqual(row["highest_tier_title"], "Обычное nothing")

    def test_larger_payment_becomes_highest_tier(self) -> None:
        self.storage.upsert_user(self.user)
        self.storage.create_or_update_pending_payment(self.user, self.plan_small, "2200")
        self.storage.submit_latest_payment(101, proof_text="one", proof_file_id=None, proof_kind="text")
        self.storage.approve_payment(1, 1)

        self.storage.create_or_update_pending_payment(self.user, self.plan_large, "2200")
        self.storage.submit_latest_payment(101, proof_text="two", proof_file_id=None, proof_kind="text")
        self.storage.approve_payment(2, 1)

        row = self.storage.get_user(101)

        self.assertEqual(row["approved_total_amount"], 1100)
        self.assertEqual(row["approved_payments_count"], 2)
        self.assertEqual(row["highest_tier_title"], "Легендарное nothing")
        self.assertEqual(row["highest_tier_amount"], 1000)

    def test_live_stats_include_top_users(self) -> None:
        self.storage.upsert_user(self.user)
        self.storage.upsert_user(self.referrer)

        self.storage.create_or_update_pending_payment(self.user, self.plan_large, "2200")
        self.storage.submit_latest_payment(101, proof_text="big", proof_file_id=None, proof_kind="text")
        self.storage.approve_payment(1, 1)

        self.storage.create_or_update_pending_payment(self.referrer, self.plan_small, "2200")
        self.storage.submit_latest_payment(202, proof_text="small", proof_file_id=None, proof_kind="text")
        self.storage.approve_payment(2, 1)

        stats = self.storage.build_live_stats(utc_now())

        self.assertEqual(stats["total_users"], 2)
        self.assertEqual(stats["paid_users"], 2)
        self.assertEqual(stats["approved_payments"], 2)
        self.assertEqual(stats["approved_amount_rub"], 1100)
        self.assertEqual(int(stats["top_users"][0]["user_id"]), 101)


if __name__ == "__main__":
    unittest.main()
