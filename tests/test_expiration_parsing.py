import importlib.util
import os
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


class FakeDynamoTable:
    def __init__(self):
        self.items = []
        self.update_calls = []

    def scan(self, **kwargs):
        return {"Items": self.items}

    def update_item(self, **kwargs):
        self.update_calls.append(kwargs)
        return {}


class FakeDynamoResource:
    def __init__(self):
        self.tables = {}

    def Table(self, name):
        return self.tables.setdefault(name, FakeDynamoTable())


class FakeSchedulerClient:
    class exceptions:
        class ConflictException(Exception):
            pass

    def __init__(self):
        self.create_calls = []

    def create_schedule(self, **kwargs):
        self.create_calls.append(kwargs)
        return {}


class FakeBoto3(types.ModuleType):
    def __init__(self):
        super().__init__("boto3")
        self.dynamodb = FakeDynamoResource()
        self.scheduler = FakeSchedulerClient()

    def resource(self, name):
        return self.dynamodb

    def client(self, name):
        if name == "scheduler":
            return self.scheduler
        return types.SimpleNamespace()


def install_fake_boto3():
    fake_boto3 = FakeBoto3()
    sys.modules["boto3"] = fake_boto3
    dynamodb_module = types.ModuleType("boto3.dynamodb")
    conditions_module = types.ModuleType("boto3.dynamodb.conditions")
    conditions_module.Attr = lambda name: types.SimpleNamespace()
    conditions_module.Key = lambda name: types.SimpleNamespace(
        eq=lambda value: (name, value)
    )
    sys.modules["boto3.dynamodb"] = dynamodb_module
    sys.modules["boto3.dynamodb.conditions"] = conditions_module
    return fake_boto3


def load_handler(module_name, lambda_dir, env):
    os.environ.update(env)
    fake_boto3 = install_fake_boto3()
    handler_path = (
        Path(__file__).resolve().parents[1] / "lambda" / lambda_dir / "handler.py"
    )
    spec = importlib.util.spec_from_file_location(module_name, handler_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, fake_boto3


class ExpirationCheckerParsingTests(unittest.TestCase):
    def setUp(self):
        self.handler, self.fake_boto3 = load_handler(
            "expiration_checker_handler_under_test",
            "expiration_checker",
            {"CERTS_TABLE": "certs"},
        )
        self.certs_table = self.fake_boto3.dynamodb.tables["certs"]

    def test_lambda_skips_no_expiry_and_malformed_values_without_crashing(self):
        self.certs_table.items = [
            {
                "employee_id": "emp-1",
                "certification_id": "cert-1",
                "expires_at": "no-expiry",
                "status": "active",
            },
            {
                "employee_id": "emp-2",
                "certification_id": "cert-2",
                "expires_at": "not-a-date",
                "status": "active",
            },
        ]

        with self.assertLogs(level="WARNING") as logs:
            result = self.handler.lambda_handler({}, None)

        self.assertEqual({"updated": 0, "total": 2}, result)
        self.assertEqual([], self.certs_table.update_calls)
        self.assertIn("Skipping invalid expiration date: not-a-date", logs.output[0])

    def test_compute_status_keeps_existing_thresholds_for_valid_dates(self):
        now = datetime.now(timezone.utc)

        self.assertEqual(
            "expired",
            self.handler.compute_status((now - timedelta(days=1)).isoformat()),
        )
        self.assertEqual(
            "critical",
            self.handler.compute_status((now + timedelta(days=30)).isoformat()),
        )
        self.assertEqual(
            "expiring_soon",
            self.handler.compute_status((now + timedelta(days=60)).isoformat()),
        )
        self.assertEqual(
            "upcoming_renewal",
            self.handler.compute_status((now + timedelta(days=90)).isoformat()),
        )
        self.assertEqual(
            "active",
            self.handler.compute_status((now + timedelta(days=92)).isoformat()),
        )


class BadgeSyncParsingTests(unittest.TestCase):
    def setUp(self):
        self.handler, self.fake_boto3 = load_handler(
            "badge_sync_handler_under_test_expiration_parsing",
            "badge_sync",
            {
                "CERTS_TABLE": "certs",
                "USERS_TABLE": "users",
                "SCHEDULER_ROLE_ARN": "scheduler-role",
                "NOTIFICATION_LAMBDA_ARN": "notification-lambda",
            },
        )

    def test_compute_status_treats_no_expiry_and_malformed_values_as_active(self):
        self.assertEqual("active", self.handler.compute_status("no-expiry"))
        with self.assertLogs(level="WARNING") as logs:
            self.assertEqual("active", self.handler.compute_status("not-a-date"))
        self.assertIn(
            "Treating invalid expiration date as active: not-a-date", logs.output[0]
        )

    def test_create_reminder_schedules_skips_invalid_expiry_when_scheduler_enabled(
        self,
    ):
        with self.assertLogs(level="WARNING") as logs:
            self.handler.create_reminder_schedules(
                "emp-1", "cert-1", "AWS Certified Example", "no-expiry"
            )
            self.handler.create_reminder_schedules(
                "emp-1", "cert-1", "AWS Certified Example", "not-a-date"
            )

        self.assertEqual([], self.fake_boto3.scheduler.create_calls)
        self.assertIn(
            "Skipping reminder schedules for non-expiring certification", logs.output[0]
        )
        self.assertIn(
            "Skipping reminder schedules for invalid expiration date: not-a-date",
            logs.output[1],
        )


if __name__ == "__main__":
    unittest.main()
