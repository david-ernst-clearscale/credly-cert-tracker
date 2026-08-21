import importlib.util
import json
import os
import sys
import types
import unittest
from pathlib import Path


class FakeDynamoResource:
    def Table(self, name):
        return types.SimpleNamespace()


class FakeBoto3(types.ModuleType):
    def __init__(self):
        super().__init__("boto3")

    def resource(self, name):
        return FakeDynamoResource()

    def client(self, name):
        return types.SimpleNamespace()


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps({"data": []}).encode("utf-8")


def load_badge_sync_handler():
    os.environ.update({"CERTS_TABLE": "certs", "USERS_TABLE": "users"})
    sys.modules["boto3"] = FakeBoto3()
    dynamodb_module = types.ModuleType("boto3.dynamodb")
    conditions_module = types.ModuleType("boto3.dynamodb.conditions")
    conditions_module.Key = lambda name: types.SimpleNamespace(
        eq=lambda value: (name, value)
    )
    sys.modules["boto3.dynamodb"] = dynamodb_module
    sys.modules["boto3.dynamodb.conditions"] = conditions_module

    handler_path = (
        Path(__file__).resolve().parents[1] / "lambda" / "badge_sync" / "handler.py"
    )
    spec = importlib.util.spec_from_file_location(
        "badge_sync_handler_under_test", handler_path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BadgeSyncSecurityTests(unittest.TestCase):
    def setUp(self):
        self.handler = load_badge_sync_handler()

    def test_fetch_credly_badges_url_encodes_username_path_segment(self):
        captured_urls = []

        def fake_urlopen(request, timeout):
            captured_urls.append(request.full_url)
            return FakeResponse()

        self.handler.urllib.request.urlopen = fake_urlopen

        self.handler.fetch_credly_badges("jane/admin?debug=true")

        self.assertEqual(
            [
                "https://www.credly.com/users/jane%2Fadmin%3Fdebug%3Dtrue/badges.json?page=1&page_size=48"
            ],
            captured_urls,
        )


if __name__ == "__main__":
    unittest.main()
