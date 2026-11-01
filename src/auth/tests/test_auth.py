import os
import unittest
from unittest.mock import patch

os.environ.setdefault("AWS_DEFAULT_REGION","ap-south-1")
os.environ.setdefault("ENVIRONMENT","test")
os.environ.setdefault("AUTH_TOKENS_TABLE","medislot-test-auth-tokens")
os.environ.setdefault("JWT_SECRET_PARAMETER","/medislot/test/auth/jwt-secret")

import jwt
from src.auth.authorizer import authorizer


class AuthorizerTests(unittest.TestCase):
    def setUp(self):
        self.claims={"sub":"101","role":"patient","iat":100,"exp":9999999999,
                     "jti":"jti-current","sid":"session-current"}
        self.event={"authorizationToken":"Bearer test-token","methodArn":"arn:aws:execute-api:region:acct:api/dev/GET/test"}

    def _patch(self, active):
        return (
            patch.object(authorizer,"get_secret",return_value="test-secret"),
            patch.object(authorizer.jwt,"decode",return_value=self.claims),
            patch.object(authorizer.table,"get_item",return_value={"Item":active} if active else {}),
        )

    def test_missing_bearer_token_is_rejected(self):
        with self.assertRaises(Exception) as raised:
            authorizer.lambda_handler({"methodArn":"arn:test"},None)
        self.assertEqual(str(raised.exception),"Unauthorized")

    def test_matching_active_token_is_allowed(self):
        p1,p2,p3=self._patch({"tokenType":"ACTIVE_ACCESS","jti":"jti-current","sessionId":"session-current"})
        with p1,p2,p3:
            result=authorizer.lambda_handler(self.event,None)
        self.assertEqual(result["principalId"],"101")
        self.assertEqual(result["context"]["role"],"patient")

    def test_old_token_is_rejected_after_replacement(self):
        p1,p2,p3=self._patch({"tokenType":"ACTIVE_ACCESS","jti":"jti-new","sessionId":"session-new"})
        with p1,p2,p3,self.assertRaises(Exception) as raised:
            authorizer.lambda_handler(self.event,None)
        self.assertEqual(str(raised.exception),"Unauthorized")

    def test_no_active_record_is_rejected(self):
        p1,p2,p3=self._patch(None)
        with p1,p2,p3,self.assertRaises(Exception) as raised:
            authorizer.lambda_handler(self.event,None)
        self.assertEqual(str(raised.exception),"Unauthorized")

    def test_invalid_jwt_is_rejected(self):
        with patch.object(authorizer,"get_secret",return_value="test-secret"), \
             patch.object(authorizer.jwt,"decode",side_effect=jwt.InvalidTokenError("bad")):
            with self.assertRaises(Exception) as raised:
                authorizer.lambda_handler(self.event,None)
        self.assertEqual(str(raised.exception),"Unauthorized")


if __name__=="__main__":
    unittest.main()
