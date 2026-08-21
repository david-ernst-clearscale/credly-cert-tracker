import base64
import importlib.util
import json
import os
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path


CSV_HEADER = "User name,User work email,Certification name,Certification level,Award date,Expiration date\n"
CSV_ROW = "Jane Doe,jane.doe@example.com,AWS Certified Solutions Architect,Associate,2024-01-01,2027-01-01\n"


class FakeS3Client:
    class exceptions:
        class NoSuchKey(Exception):
            pass

    def __init__(self):
        self.objects = []

    def put_object(self, **kwargs):
        self.objects.append(kwargs)
        return {}

    def get_object(self, **kwargs):
        raise self.exceptions.NoSuchKey()


class FakeDynamoTable:
    def __init__(self):
        self.items = []
        self.scan_pages = None
        self.scan_calls = []
        self.update_calls = []

    def scan(self, **kwargs):
        self.scan_calls.append(kwargs)
        if self.scan_pages is not None:
            return self.scan_pages[len(self.scan_calls) - 1]
        return {"Items": self.items}

    def update_item(self, **kwargs):
        self.update_calls.append(kwargs)
        return {}


class FakeDynamoResource:
    def __init__(self):
        self.tables = {}

    def Table(self, name):
        return self.tables.setdefault(name, FakeDynamoTable())


class FakeBoto3(types.ModuleType):
    def __init__(self):
        super().__init__("boto3")
        self.s3_client = FakeS3Client()
        self.dynamodb = FakeDynamoResource()

    def resource(self, name):
        return self.dynamodb

    def client(self, name):
        if name == "s3":
            return self.s3_client
        return types.SimpleNamespace()


def load_handler():
    os.environ.update(
        {
            "CERTS_TABLE": "certs",
            "USERS_TABLE": "users",
            "ROSTER_BUCKET": "roster-bucket",
            "ADMIN_EMAILS": "admin@example.com",
        }
    )
    fake_boto3 = FakeBoto3()
    sys.modules["boto3"] = fake_boto3
    dynamodb_module = types.ModuleType("boto3.dynamodb")
    conditions_module = types.ModuleType("boto3.dynamodb.conditions")
    conditions_module.Key = lambda name: types.SimpleNamespace(
        eq=lambda value: (name, value)
    )
    sys.modules["boto3.dynamodb"] = dynamodb_module
    sys.modules["boto3.dynamodb.conditions"] = conditions_module

    handler_path = (
        Path(__file__).resolve().parents[1] / "lambda" / "dashboard_api" / "handler.py"
    )
    spec = importlib.util.spec_from_file_location(
        "dashboard_api_handler_under_test", handler_path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, fake_boto3.s3_client


def admin_event(body):
    return {
        "requestContext": {"authorizer": {"claims": {"email": "admin@example.com"}}},
        "body": json.dumps(body),
    }


def roster_event(body, email="admin@example.com", **overrides):
    event = {
        "requestContext": {"authorizer": {"claims": {"email": email}}},
        "body": body,
    }
    event.update(overrides)
    return event


class DashboardApiSecurityTests(unittest.TestCase):
    def setUp(self):
        self.handler, self.s3 = load_handler()

    def test_roster_upload_rejects_decoded_body_over_byte_limit(self):
        oversized_csv = CSV_HEADER + ("x" * (10 * 1024 * 1024 + 1))
        response = self.handler.handle_roster_upload(roster_event(oversized_csv))

        self.assertEqual(413, response["statusCode"])
        body = json.loads(response["body"])
        self.assertIn("too large", body["error"].lower())
        self.assertEqual([], self.s3.objects)

    def test_roster_upload_rejects_base64_decoded_body_over_byte_limit(self):
        oversized_bytes = (CSV_HEADER + ("x" * (10 * 1024 * 1024 + 1))).encode("utf-8")
        response = self.handler.handle_roster_upload(
            roster_event(
                base64.b64encode(oversized_bytes).decode("ascii"),
                isBase64Encoded=True,
            )
        )

        self.assertEqual(413, response["statusCode"])
        body = json.loads(response["body"])
        self.assertIn("too large", body["error"].lower())
        self.assertEqual([], self.s3.objects)

    def test_parse_apn_csv_rejects_absurd_row_counts(self):
        csv_text = CSV_HEADER + (CSV_ROW * 25001)

        with self.assertRaisesRegex(ValueError, "25,000"):
            self.handler.parse_apn_csv(csv_text)

    def test_roster_upload_accepts_normal_small_csv(self):
        response = self.handler.handle_roster_upload(roster_event(CSV_HEADER + CSV_ROW))

        self.assertEqual(200, response["statusCode"])
        body = json.loads(response["body"])
        self.assertTrue(body["ok"])
        self.assertEqual(1, body["named_people"])
        self.assertEqual(0, body["redacted_count"])
        self.assertEqual(1, len(self.s3.objects))

    def test_roster_upload_rejects_non_admin_authenticated_caller(self):
        response = self.handler.handle_roster_upload(
            roster_event(CSV_HEADER + CSV_ROW, email="analyst@example.com")
        )

        self.assertEqual(403, response["statusCode"])
        body = json.loads(response["body"])
        self.assertIn("permission", body["error"].lower())
        self.assertEqual([], self.s3.objects)

    def test_upsert_user_rejects_path_like_credly_username(self):
        response = self.handler.handle_upsert_user(
            admin_event(
                {"employee_id": "jane.doe", "credly_username": "jane/admin?debug=true"}
            )
        )

        self.assertEqual(400, response["statusCode"])
        body = json.loads(response["body"])
        self.assertIn("credly_username", body["error"])

    def test_upsert_user_rejects_path_like_employee_id(self):
        response = self.handler.handle_upsert_user(
            admin_event({"employee_id": "team/jane", "credly_username": "jane.doe"})
        )

        self.assertEqual(400, response["statusCode"])
        body = json.loads(response["body"])
        self.assertIn("employee_id", body["error"])

    def test_upsert_user_accepts_and_normalizes_normal_values(self):
        response = self.handler.handle_upsert_user(
            admin_event(
                {
                    "employee_id": "  jane.doe@example.com  ",
                    "credly_username": "  jane.doe_aws-1  ",
                }
            )
        )

        self.assertEqual(200, response["statusCode"])
        body = json.loads(response["body"])
        self.assertEqual("jane.doe@example.com", body["user"]["employee_id"])
        self.assertEqual("jane.doe_aws-1", body["user"]["credly_username"])

        users_table = self.handler.dynamodb.Table("users")
        self.assertEqual(1, len(users_table.update_calls))
        self.assertEqual(
            {"employee_id": "jane.doe@example.com"},
            users_table.update_calls[0]["Key"],
        )
        self.assertEqual(
            {":c": "jane.doe_aws-1"},
            users_table.update_calls[0]["ExpressionAttributeValues"],
        )

    def test_is_active_rejects_bad_expiry_and_malformed_dates(self):
        self.assertFalse(
            self.handler.is_active(
                {"status": "bad_expiry", "expires_at": "2099-01-01T00:00:00+00:00"}
            )
        )
        self.assertFalse(self.handler.is_active({"expires_at": "not-a-date"}))
        self.assertTrue(self.handler.is_active({"expires_at": "no-expiry"}))
        self.assertTrue(self.handler.is_active({"expires_at": ""}))

    def test_is_active_normalizes_z_suffix_before_parsing(self):
        original_datetime = self.handler.datetime

        class RuntimeWithoutBareZ:
            @staticmethod
            def fromisoformat(value):
                if value.endswith("Z"):
                    raise ValueError("bare Z unsupported")
                return datetime.fromisoformat(value)

            @staticmethod
            def now(tz=None):
                return datetime(2025, 1, 1, tzinfo=tz or timezone.utc)

        self.handler.datetime = RuntimeWithoutBareZ
        try:
            self.assertTrue(
                self.handler.is_active({"expires_at": "2099-01-01T00:00:00Z"})
            )
        finally:
            self.handler.datetime = original_datetime

    def test_compliance_exposes_bad_expiry_warnings_without_counting_them(self):
        certs_table = self.handler.dynamodb.Table("certs")
        certs_table.items = [
            {
                "employee_id": "jane.doe",
                "certification_id": "cert-valid",
                "certification_name": "AWS Certified Cloud Practitioner",
                "expires_at": "2099-01-01T00:00:00+00:00",
                "status": "active",
            },
            {
                "employee_id": "john.smith",
                "certification_id": "cert-bad",
                "certification_name": "AWS Certified Solutions Architect - Associate",
                "expires_at": "not-a-date",
                "status": "bad_expiry",
            },
        ]

        response = self.handler.handle_compliance({})

        self.assertEqual(200, response["statusCode"])
        body = json.loads(response["body"])
        self.assertEqual(
            [
                {
                    "name": "AWS Certified Solutions Architect - Associate",
                    "employee": "john.smith",
                    "expires_at": "not-a-date",
                    "status": "bad_expiry",
                }
            ],
            body["expiry_warnings"],
        )
        self.assertEqual(1, body["aws_tiers"]["Foundational"]["current"])
        self.assertEqual(1, body["aws_tiers"]["Foundational"]["cert_count"])
        self.assertEqual([], body["aws_tiers"]["Technical"]["certifications"])
        self.assertEqual(0, body["aws_tiers"]["Technical"]["current"])
        self.assertEqual(
            [{"employee": "jane.doe", "count": 1, "rank": 1}],
            body["leaderboard"]["aws"],
        )

    def test_compliance_includes_certifications_from_all_scan_pages(self):
        certs_table = self.handler.dynamodb.Table("certs")
        certs_table.scan_pages = [
            {
                "Items": [
                    {
                        "employee_id": "jane.doe",
                        "certification_id": "cert-page-1",
                        "certification_name": "AWS Certified Cloud Practitioner",
                        "expires_at": "2099-01-01T00:00:00+00:00",
                        "status": "active",
                    }
                ],
                "LastEvaluatedKey": {"employee_id": "jane.doe"},
            },
            {
                "Items": [
                    {
                        "employee_id": "john.smith",
                        "certification_id": "cert-page-2",
                        "certification_name": "AWS Certified Cloud Practitioner",
                        "expires_at": "2099-01-01T00:00:00+00:00",
                        "status": "active",
                    }
                ]
            },
        ]

        response = self.handler.handle_compliance({})

        self.assertEqual(200, response["statusCode"])
        body = json.loads(response["body"])
        self.assertEqual(
            [{}, {"ExclusiveStartKey": {"employee_id": "jane.doe"}}],
            certs_table.scan_calls,
        )
        self.assertEqual(2, body["aws_tiers"]["Foundational"]["current"])
        self.assertEqual(2, body["aws_tiers"]["Foundational"]["cert_count"])
        self.assertEqual(
            [
                {"employee": "jane.doe", "count": 1, "rank": 1},
                {"employee": "john.smith", "count": 1, "rank": 1},
            ],
            body["leaderboard"]["aws"],
        )


if __name__ == "__main__":
    unittest.main()
