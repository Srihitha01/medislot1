import unittest
import importlib.util
import sys
from pathlib import Path

API_DIR = Path(__file__).resolve().parents[1]

# Make sibling modules such as availability_rules importable.
sys.path.insert(0, str(API_DIR))

spec = importlib.util.spec_from_file_location(
    "api_app",
    API_DIR / "app.py"
)

api = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = api
spec.loader.exec_module(api)
class PatientAuthorizationTests(unittest.TestCase):
    def event(self, path, role, uid="11"):
        return {"httpMethod": "GET", "path": path, "requestContext": {"authorizer": {"userId": uid, "role": role}}}
    def test_cross_patient_id_forbidden(self):
        result = api.lambda_handler(self.event("/patients/22", "patient"), None)
        self.assertEqual(result["statusCode"], 403)
    def test_unauthenticated_patient_forbidden(self):
        result = api.lambda_handler(self.event("/patients/me", "patient", ""), None)
        self.assertEqual(result["statusCode"], 401)
    def test_doctor_cannot_access_patient_profile(self):
        result = api.lambda_handler(self.event("/patients/me", "doctor"), None)
        self.assertEqual(result["statusCode"], 403)
    def test_search_pagination_bounds(self):
        result = api.lambda_handler({"httpMethod":"GET","path":"/doctors/search",
            "queryStringParameters":{"pageSize":"1000"},
            "requestContext":{"authorizer":{"userId":"11","role":"patient"}}},None)
        self.assertEqual(result["statusCode"],400)

if __name__ == "__main__":
    unittest.main()
