"""Firebase ID-token verification using Google's public certificate verifier.

Google-auth checks signature, expiry, issued-at and audience. Firebase's
additional issuer, subject and auth_time constraints are checked below.
No administrative Firebase operations are used: identity becomes a local
revocable cookie session. No service-account private key is required.
"""

import time
from collections.abc import Callable
from functools import partial

from google.auth import jwt
from google.auth.transport.requests import Request
from google.oauth2 import id_token

from app.config import get_settings

IdTokenVerifier = Callable[[str], dict]


def _live_verifier(token: str) -> dict:
    project_id = get_settings().firebase_project_id
    if not project_id:
        raise RuntimeError("FIREBASE_PROJECT_ID не задан")
    # Inspect only header constraints before cryptographic verification.
    # No claims from this step are trusted or used as identity.
    header = jwt.decode_header(token)
    if header.get("alg") != "RS256" or not header.get("kid"):
        raise ValueError("Invalid Firebase token header")
    claims = dict(
        id_token.verify_firebase_token(
            token, partial(Request(), timeout=10), audience=project_id
        )
    )
    subject = claims.get("sub")
    auth_time = claims.get("auth_time")
    firebase = claims.get("firebase")
    if (
        not isinstance(firebase, dict)
        or firebase.get("sign_in_provider") != "google.com"
        or claims.get("iss") != f"https://securetoken.google.com/{project_id}"
        or not isinstance(subject, str)
        or not 1 <= len(subject) <= 128
        or not isinstance(auth_time, (int, float))
        or isinstance(auth_time, bool)
        or not 0 <= auth_time <= time.time()
    ):
        raise ValueError("Invalid Firebase token claims")
    return claims


def get_google_verifier() -> IdTokenVerifier:
    """Injectable verifier; tokens are never persisted as application sessions."""
    return _live_verifier
