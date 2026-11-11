"""S3 helpers bound to one bucket: bytes with SHA-256 checksums, presigned downloads, and the
original text conveniences. Every call, body reads and listing pages included, runs on a thread
pool.

Nothing here is atomic with DynamoDB: an object written for an item exists before the item that
names it, and outlives one that stops naming it, unless the application cleans it up (docs/aws.md).
"""

import asyncio
import base64
import hashlib
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Any, AsyncIterator, Dict, List, Mapping, Optional
from urllib.parse import quote

from botocore.config import Config
from botocore.exceptions import ClientError, FlexibleChecksumError

from . import clients

logger = logging.getLogger(__name__)

# Create a thread pool with a reasonable number of workers
executor = ThreadPoolExecutor(max_workers=10)

# s3v4 makes presigned URLs SigV4: without it botocore presigns some regions with SigV2, which
# buckets created since 2020 refuse.
_CONFIG = Config(
    max_pool_connections=50,
    retries={"max_attempts": 3, "mode": "standard"},
    signature_version="s3v4",
)
# Presigned SigV4 URLs live at most a week; temporary credentials end them sooner.
MAX_PRESIGN_SECONDS = 7 * 24 * 3600
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
# Error codes S3 answers for a missing key. HEAD responses have no body, so a HEAD names only the
# status: a missing bucket reads as a missing key there.
_NOT_FOUND_CODES = frozenset({"NoSuchKey", "404", "NotFound"})


class S3ObjectNotFoundError(ClientError):
  """The key does not exist in the bucket. A ClientError still, with S3's own error response, so
  callers that catch ClientError and read its code keep working."""


class S3IntegrityError(Exception):
  """The bytes do not match the SHA-256 they were expected to have."""


class S3DeleteError(Exception):
  """DeleteObjects answered 200 but refused some keys: `errors` holds S3's entries for them."""

  def __init__(self, errors: List[Dict[str, Any]], deleted_count: int):
    super().__init__(f"S3 refused to delete {len(errors)} objects, first: {errors[0]}")
    self.errors = errors
    self.deleted_count = deleted_count


@dataclass(frozen=True)
class S3PutResult:
  key: str
  sha256: str
  etag: Optional[str]
  version_id: Optional[str]


@dataclass(frozen=True)
class S3ObjectInfo:
  key: str
  size: int
  content_type: Optional[str]
  etag: Optional[str]
  last_modified: Optional[datetime]
  sha256: Optional[str]
  metadata: Dict[str, str]
  version_id: Optional[str]


@dataclass(frozen=True)
class S3ObjectSummary:
  key: str
  size: int
  last_modified: datetime
  etag: Optional[str]


def _is_not_found(error: ClientError) -> bool:
  return error.response.get("Error", {}).get("Code") in _NOT_FOUND_CODES


def _not_found(error: ClientError) -> S3ObjectNotFoundError:
  return S3ObjectNotFoundError(error.response, error.operation_name)


def _sha256_hex(data: bytes) -> str:
  return hashlib.sha256(data).hexdigest()


def _checked_sha256(sha256: str) -> str:
  if not _SHA256_HEX.fullmatch(sha256):
    raise ValueError("sha256 must be 64 lowercase hex digits")
  return sha256


def _hex_from_b64(checksum: Optional[str]) -> Optional[str]:
  # A multipart object's checksum ends in -<parts>: a checksum of checksums, not of the bytes.
  if not checksum or "-" in checksum:
    return None
  return base64.b64decode(checksum).hex()


def attachment_disposition(filename: str) -> str:
  """A Content-Disposition naming a download `filename`, safe as a header: an ASCII fallback, and
  the exact name in RFC 5987 form for browsers that read it."""
  name = filename.replace("/", "_").replace("\\", "_")
  fallback = "".join(c if 32 <= ord(c) < 127 and c != '"' else "_" for c in name) or "download"
  exact = "".join(c for c in name if c.isprintable()) or "download"
  return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(exact, safe='')}"


class S3Client:
  """S3 calls on one bucket at one destination (profile, region, endpoint). Clients for the same
  destination share one boto3 client, whatever their bucket."""

  def __init__(
      self,
      bucket_name: str,
      profile_name: str = "",
      *,
      region_name: Optional[str] = None,
      endpoint_url: Optional[str] = None,
  ):
    if not bucket_name:
      raise ValueError("S3Client needs a bucket name")
    self.bucket_name = bucket_name
    self.target = clients.AwsTarget(profile_name or None, region_name, endpoint_url)

  def __repr__(self) -> str:
    return f"S3Client({self.bucket_name!r}, {self.target!r})"

  @property
  def client(self) -> Any:
    """The shared low-level boto3 client for this destination."""
    return clients.shared_client("s3", self.target, _CONFIG)

  async def __aenter__(self):
    """Async context manager entry."""
    return self

  async def __aexit__(self, exc_type, exc, tb):
    """Async context manager exit."""
    pass

  # --- bytes

  async def put_bytes(
      self,
      key: str,
      data: bytes,
      *,
      content_type: str = "application/octet-stream",
      sha256: Optional[str] = None,
      metadata: Optional[Mapping[str, str]] = None,
  ) -> S3PutResult:
    """Store `data` under `key` in one PutObject with its SHA-256, which S3 checks on receipt and
    keeps with the object. `sha256` (hex), when given, must be the digest of `data`."""
    digest = _sha256_hex(data)
    if sha256 is not None and _checked_sha256(sha256) != digest:
      raise S3IntegrityError(f"{key}: data does not match the sha256 it was given")
    params: Dict[str, Any] = {
        "Bucket": self.bucket_name,
        "Key": key,
        "Body": bytes(data),
        "ContentType": content_type,
        "ChecksumSHA256": base64.b64encode(bytes.fromhex(digest)).decode(),
    }
    if metadata:
      params["Metadata"] = dict(metadata)
    response = await self._call("put_object", **params)
    return S3PutResult(key, digest, response.get("ETag"), response.get("VersionId"))

  async def get_bytes(self, key: str, *, expected_sha256: Optional[str] = None) -> bytes:
    """The object's bytes, read on the thread pool. Raises S3ObjectNotFoundError for a missing key,
    and S3IntegrityError when the bytes are not those S3's stored checksum or `expected_sha256`
    describe."""
    def _read() -> bytes:
      # ChecksumMode has botocore check the stored checksum as the body is read.
      response = self.client.get_object(Bucket=self.bucket_name, Key=key, ChecksumMode="ENABLED")
      return response["Body"].read()

    try:
      data = await self._run_in_executor(_read)
    except ClientError as e:
      if _is_not_found(e):
        raise _not_found(e) from e
      raise
    except FlexibleChecksumError as e:
      raise S3IntegrityError(f"{key}: {e}") from e
    if expected_sha256 is not None and _sha256_hex(data) != _checked_sha256(expected_sha256):
      raise S3IntegrityError(f"{key}: bytes do not match the expected sha256")
    return data

  async def head_object(self, key: str) -> Optional[S3ObjectInfo]:
    """The object's metadata, its SHA-256 when it was stored with one, or None for a missing key.
    Any other failure, such as AccessDenied, is raised. Without s3:ListBucket, S3 answers 403 for a
    missing key, which raises too."""
    try:
      response = await self._call(
          "head_object", Bucket=self.bucket_name, Key=key, ChecksumMode="ENABLED"
      )
    except ClientError as e:
      if _is_not_found(e):
        return None
      raise
    return S3ObjectInfo(
        key=key,
        size=response.get("ContentLength", 0),
        content_type=response.get("ContentType"),
        etag=response.get("ETag"),
        last_modified=response.get("LastModified"),
        sha256=_hex_from_b64(response.get("ChecksumSHA256")),
        metadata=response.get("Metadata", {}),
        version_id=response.get("VersionId"),
    )

  async def object_exists(self, key: str) -> bool:
    """Whether the key exists; raised, not False, when S3 refuses to say (see head_object)."""
    return await self.head_object(key) is not None

  async def delete_object(self, key: str) -> None:
    """Delete the key. Deleting a missing key succeeds, as S3 does."""
    await self._call("delete_object", Bucket=self.bucket_name, Key=key)

  async def iter_objects(
      self, prefix: str = "", *, page_size: int = 1000
  ) -> AsyncIterator[S3ObjectSummary]:
    """Every object under `prefix`, a ListObjectsV2 page of at most `page_size` keys at a time,
    each page fetched on the thread pool."""
    params: Dict[str, Any] = {"Bucket": self.bucket_name, "Prefix": prefix, "MaxKeys": page_size}
    while True:
      page = await self._call("list_objects_v2", **params)
      for obj in page.get("Contents", []):
        yield S3ObjectSummary(obj["Key"], obj.get("Size", 0), obj["LastModified"], obj.get("ETag"))
      if not page.get("IsTruncated"):
        return
      params["ContinuationToken"] = page["NextContinuationToken"]

  async def presigned_get_url(
      self,
      key: str,
      *,
      expires_in: int,
      filename: Optional[str] = None,
      content_type: Optional[str] = None,
  ) -> str:
    """A GET URL for the key that works without credentials for `expires_in` seconds, downloaded as
    an attachment named `filename` when given. The URL is a bearer credential: do not log it.
    Signing checks neither that the key exists nor that this client may read it."""
    if not 1 <= expires_in <= MAX_PRESIGN_SECONDS:
      raise ValueError(f"expires_in must be between 1 and {MAX_PRESIGN_SECONDS} seconds")
    params: Dict[str, Any] = {"Bucket": self.bucket_name, "Key": key}
    if filename:
      params["ResponseContentDisposition"] = attachment_disposition(filename)
    if content_type:
      params["ResponseContentType"] = content_type
    # On the thread pool: signing may first fetch credentials (instance role, SSO).
    return await self._run_in_executor(
        lambda: self.client.generate_presigned_url(
            "get_object", Params=params, ExpiresIn=expires_in
        )
    )

  # --- text documents

  async def upload_document(self, key: str, content: str, content_type: str = "text/plain") -> str:
    """Upload document content to S3 and return the S3 URL."""
    await self._call(
        "put_object",
        Bucket=self.bucket_name,
        Key=key,
        Body=content.encode("utf-8"),
        ContentType=content_type,
    )
    return f"s3://{self.bucket_name}/{key}"

  async def download_document(self, key: str) -> str:
    """Download document content from S3; S3ObjectNotFoundError for a missing key."""
    return (await self.get_bytes(key)).decode("utf-8")

  async def delete_document(self, key: str) -> None:
    """Delete document from S3."""
    await self.delete_object(key)

  async def document_exists(self, key: str) -> bool:
    """Check if document exists in S3. Permission and service errors are raised: they say nothing
    about whether it exists."""
    return await self.object_exists(key)

  async def delete_folder(self, prefix: str) -> int:
    """Delete all objects with the given prefix (folder) and return count of deleted objects.
    Raises S3DeleteError if S3 refused any key."""
    deleted_count = 0
    batch: List[Dict[str, str]] = []
    async for obj in self.iter_objects(prefix):
      batch.append({"Key": obj.key})
      # DeleteObjects takes at most 1000 keys, as many as a listing page holds.
      if len(batch) == 1000:
        deleted_count += await self._delete_batch(batch, deleted_count)
        batch = []
    if batch:
      deleted_count += await self._delete_batch(batch, deleted_count)
    return deleted_count

  async def _delete_batch(self, batch: List[Dict[str, str]], deleted_so_far: int) -> int:
    response = await self._call(
        "delete_objects", Bucket=self.bucket_name, Delete={"Objects": batch, "Quiet": True}
    )
    errors = response.get("Errors", [])
    if errors:
      raise S3DeleteError(errors, deleted_so_far + len(batch) - len(errors))
    return len(batch)

  async def _call(self, operation: str, **params: Any) -> Any:
    """A low-level client call on the thread pool, where the client is also made on first use."""
    return await self._run_in_executor(lambda: getattr(self.client, operation)(**params))

  async def _run_in_executor(self, func):
    """Run a blocking function in the thread pool executor."""
    return await asyncio.get_running_loop().run_in_executor(executor, func)
