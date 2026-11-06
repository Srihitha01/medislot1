import importlib.util
import json
import os
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("medislot_api", os.path.join(os.path.dirname(__file__), "..", "app.py"))
api = importlib.util.module_from_spec(spec)
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
