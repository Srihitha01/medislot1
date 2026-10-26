
import os
import unittest
from unittest.mock import patch

# Set environment variables before importing the Lambda module.
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("AUTH_TOKENS_TABLE", "medislot-test-auth-tokens")
os.environ.setdefault("JWT_SECRET_PARAMETER", "/medislot/test/auth/jwt-secret")

import bcrypt
import jwt

from src.auth.authorizer import authorizer


class PasswordTests(unittest.TestCase):
    def test_bcrypt_hash_matches_password(self):
        password = b"ExamplePassword123"
        password_hash = bcrypt.hashpw(password, bcrypt.gensalt(rounds=12))

        self.assertTrue(bcrypt.checkpw(password, password_hash))

    def test_bcrypt_rejects_wrong_password(self):
        password_hash = bcrypt.hashpw(
            b"CorrectPassword123",
            bcrypt.gensalt(rounds=12),
        )

        self.assertFalse(
            bcrypt.checkpw(b"WrongPassword123", password_hash)
        )


class AuthorizerTests(unittest.TestCase):
    def test_missing_bearer_token_is_rejected(self):
        event = {
            "methodArn": "arn:aws:execute-api:ap-south-1:123456789012:api/dev/GET/doctors"
        }

        with self.assertRaises(Exception) as raised:
            authorizer.lambda_handler(event, None)

        self.assertEqual(str(raised.exception), "Unauthorized")

    def test_valid_token_allows_request_and_sets_context(self):
        claims = {
            "sub": "user-123",
            "role": "patient",
            "iat": 100,
            "exp": 9999999999,
            "jti": "token-jti-123",
        }

        event = {
            "authorizationToken": "Bearer example-token",
            "methodArn": "arn:aws:execute-api:ap-south-1:123456789012:api/dev/GET/doctors",
        }

        with (
            patch.object(authorizer, "get_jwt_secret", return_value="test-secret"),
            patch.object(authorizer.jwt, "decode", return_value=claims),
            patch.object(
                authorizer.table,
                "get_item",
                return_value={},
            ),
        ):
            result = authorizer.lambda_handler(event, None)

        self.assertEqual(result["principalId"], "user-123")
        self.assertEqual(
            result["policyDocument"]["Statement"][0]["Effect"],
            "Allow",
        )
        self.assertEqual(result["context"]["userId"], "user-123")
        self.assertEqual(result["context"]["role"], "patient")

    def test_revoked_token_is_rejected(self):
        claims = {
            "sub": "user-123",
            "role": "patient",
            "iat": 100,
            "exp": 9999999999,
            "jti": "revoked-jti",
        }

        event = {
            "authorizationToken": "Bearer example-token",
            "methodArn": "arn:aws:execute-api:ap-south-1:123456789012:api/dev/GET/doctors",
        }

        with (
            patch.object(authorizer, "get_jwt_secret", return_value="test-secret"),
            patch.object(authorizer.jwt, "decode", return_value=claims),
            patch.object(
                authorizer.table,
                "get_item",
                return_value={
                    "Item": {
                        "tokenId": "revoked-jti",
                        "tokenType": "ACCESS_DENYLIST",
                        "expiresAt": 9999999999,
                    }
                },
            ),
        ):
            with self.assertRaises(Exception) as raised:
                authorizer.lambda_handler(event, None)

        self.assertEqual(str(raised.exception), "Unauthorized")

    def test_invalid_jwt_is_rejected(self):
        event = {
            "authorizationToken": "Bearer tampered-token",
            "methodArn": "arn:aws:execute-api:ap-south-1:123456789012:api/dev/GET/doctors",
        }

        with (
            patch.object(authorizer, "get_jwt_secret", return_value="test-secret"),
            patch.object(
                authorizer.jwt,
                "decode",
                side_effect=jwt.InvalidTokenError("Invalid signature"),
            ),
        ):
            with self.assertRaises(Exception) as raised:
                authorizer.lambda_handler(event, None)

        self.assertEqual(str(raised.exception), "Unauthorized")


if __name__ == "__main__":
    unittest.main()
