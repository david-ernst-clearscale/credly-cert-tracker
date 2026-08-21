import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


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


def load_module(module_name, path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    original_path = list(sys.path)
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = original_path
    return module


def install_boto3_fakes():
    sys.modules["boto3"] = FakeBoto3()
    dynamodb_module = types.ModuleType("boto3.dynamodb")
    conditions_module = types.ModuleType("boto3.dynamodb.conditions")
    conditions_module.Key = lambda name: types.SimpleNamespace(
        eq=lambda value: (name, value)
    )
    conditions_module.Attr = lambda name: types.SimpleNamespace(
        ne=lambda value: (name, value)
    )
    sys.modules["boto3.dynamodb"] = dynamodb_module
    sys.modules["boto3.dynamodb.conditions"] = conditions_module


def load_handler(lambda_name):
    os.environ.update({"CERTS_TABLE": "certs", "USERS_TABLE": "users"})
    install_boto3_fakes()
    return load_module(
        f"{lambda_name}_handler_classifier_test",
        REPO_ROOT / "lambda" / lambda_name / "handler.py",
    )


class CertificationClassifierTests(unittest.TestCase):
    def load_classifier(self, lambda_name):
        return load_module(
            f"{lambda_name}_cert_classifier_test",
            REPO_ROOT / "lambda" / lambda_name / "cert_classifier.py",
        )

    def test_copied_lambda_helpers_keep_shared_classification_contract(self):
        helpers = [
            self.load_classifier("badge_sync"),
            self.load_classifier("dashboard_api"),
            self.load_classifier("compliance_reporter"),
        ]

        aws_cases = {
            "AWS Certified Cloud Practitioner": "Foundational",
            "AWS Certified AI Practitioner": "Foundational",
            "AWS Certified Solutions Architect - Associate": "Technical",
            "AWS Certified Security - Specialty": "Professional/Specialty",
            "AWS Certified Solutions Architect - Professional": "Professional/Specialty",
        }
        claude_cases = {
            "Claude Certified Architect - Foundations": "CCAR-F",
            "Claude Certified Architect - Professional": "CCAR-P",
            "Claude Certified Developer - Foundations": "CCDV-F",
            "Claude Certified Associate - Foundations": "CCAO-F",
            "Claude Certified Totally New Credential": "Unknown",
        }

        for helper in helpers:
            self.assertEqual(
                {"Foundational": 10, "Technical": 25, "Professional/Specialty": 10},
                helper.AWS_REQS,
            )
            self.assertEqual(
                {"CCAR-F": 10, "CCAR-P": 0, "CCDV-F": 0, "CCAO-F": 0},
                helper.CLAUDE_REQS,
            )
            self.assertNotIn("Unknown", helper.CLAUDE_REQS)
            for name, category in aws_cases.items():
                self.assertTrue(helper.is_real_cert(name))
                self.assertEqual(category, helper.classify_aws(name))
                self.assertEqual(category, helper.classify_certification(name))
            for name, category in claude_cases.items():
                self.assertTrue(helper.is_real_cert(name))
                self.assertEqual(category, helper.classify_claude(name))
                self.assertEqual(category, helper.classify_certification(name))

            self.assertFalse(
                helper.is_real_cert("AWS Certified AI Practitioner Early Adopter")
            )
            self.assertIsNone(
                helper.classify_certification(
                    "AWS Certified AI Practitioner Early Adopter"
                )
            )
            self.assertFalse(
                helper.is_certification_badge(
                    {"badge_template": {"name": "Knowledge Badge"}}
                )
            )
            self.assertTrue(
                helper.is_certification_badge(
                    {
                        "badge_template": {
                            "name": "Claude Certified Developer - Foundations"
                        }
                    }
                )
            )

    def test_badge_sync_and_dashboard_expose_same_shared_classifier_semantics(self):
        badge_sync = load_handler("badge_sync")
        dashboard = load_handler("dashboard_api")

        shared_cases = {
            "AWS Certified Cloud Practitioner": "Foundational",
            "AWS Certified Solutions Architect - Associate": "Technical",
            "AWS Certified Security - Specialty": "Professional/Specialty",
            "Claude Certified Architect - Foundations": "CCAR-F",
            "Claude Certified Architect - Professional": "CCAR-P",
            "Claude Certified Developer - Foundations": "CCDV-F",
            "Claude Certified Associate - Foundations": "CCAO-F",
            "Claude Certified Totally New Credential": "Unknown",
            "AWS Certified AI Practitioner Early Adopter": None,
        }

        for name, category in shared_cases.items():
            self.assertEqual(category, badge_sync.classify_certification(name))
            self.assertEqual(category, dashboard.classify_certification(name))


if __name__ == "__main__":
    unittest.main()
