import logging
import os
from functools import lru_cache
from typing import Any, Dict, Optional

import jwt
from jwt import PyJWKClient
from jwt.exceptions import ExpiredSignatureError

logger = logging.getLogger(__name__)


def verify_clerk_jwt(
  token: str,
  issuer: Optional[str] = None,
  audience: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
  if token.count(".") != 2:
    return None

  issuer = _resolve_issuer(issuer)
  audience = _resolve_audience(audience)
  jwk_client = _get_jwk_client(issuer)

  try:
    signing_key = jwk_client.get_signing_key_from_jwt(token)
    decoded = jwt.decode(
      token,
      signing_key.key,
      algorithms=["RS256"],
      issuer=issuer,
      audience=audience or None,
    )
  except ExpiredSignatureError:
    return None
  except Exception as exc:
    logger.info("Invalid Clerk JWT", extra={"error": str(exc)})
    return None

  return _normalize_claims(decoded)


def _normalize_claims(decoded: Dict[str, Any]) -> Optional[Dict[str, Any]]:
  user_id = decoded.get("user_id") or decoded.get("sub")
  if not user_id:
    return None

  return {
    "user_id": user_id,
    "first_name": decoded.get("first_name"),
    "last_name": decoded.get("last_name"),
    "email": decoded.get("email"),
    "phone": decoded.get("phone"),
    "image_url": decoded.get("image_url"),
  }


def _resolve_issuer(issuer: Optional[str]) -> str:
  if issuer:
    return issuer
  env_issuer = os.getenv("CLERK_JWT_ISSUER")
  if not env_issuer:
    raise EnvironmentError("CLERK_JWT_ISSUER environment variable is not set")
  return env_issuer


def _resolve_audience(audience: Optional[str]) -> Optional[str]:
  if audience is not None:
    return audience
  env_audience = os.getenv("CLERK_JWT_AUDIENCE")
  return env_audience or None


@lru_cache(maxsize=8)
def _get_jwk_client(issuer: str) -> PyJWKClient:
  jwks_url = f"{issuer}/.well-known/jwks.json"
  return PyJWKClient(jwks_url)
