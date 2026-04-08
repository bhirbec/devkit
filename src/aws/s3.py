import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# Create a thread pool with a reasonable number of workers
executor = ThreadPoolExecutor(max_workers=10)


class S3Client:
  """S3 client for document storage operations."""

  _instance = None
  _s3_client: Any = None
  _bucket_name: str = ""

  def __new__(cls, bucket_name: str, profile_name: str = ""):
    if cls._instance is None:
      cls._instance = super(S3Client, cls).__new__(cls)
      # Configure connection pooling
      config = Config(
          max_pool_connections=50,  # Adjust based on workload
          retries={"max_attempts": 3, "mode": "standard"}
      )

      # Initialize the S3 client with optional profile and region
      if profile_name:
        session = boto3.Session(profile_name=profile_name)
        cls._s3_client = session.client("s3", config=config)
      else:
        cls._s3_client = boto3.client("s3", config=config)

      # Set bucket name from parameter
      cls._bucket_name = bucket_name
    return cls._instance

  async def __aenter__(self):
    """Async context manager entry."""
    return self

  async def __aexit__(self, exc_type, exc, tb):
    """Async context manager exit."""
    pass

  async def upload_document(self, key: str, content: str, content_type: str = "text/plain") -> str:
    """Upload document content to S3 and return the S3 URL."""
    await self._run_in_executor(
        lambda: self._s3_client.put_object(
            Bucket=self._bucket_name,
            Key=key,
            Body=content.encode("utf-8"),
            ContentType=content_type,
        )
    )
    return f"s3://{self._bucket_name}/{key}"

  async def download_document(self, key: str) -> str:
    """Download document content from S3."""
    response = await self._run_in_executor(
        lambda: self._s3_client.get_object(
            Bucket=self._bucket_name,
            Key=key,
        )
    )
    return response["Body"].read().decode("utf-8")

  async def delete_document(self, key: str) -> None:
    """Delete document from S3."""
    await self._run_in_executor(
        lambda: self._s3_client.delete_object(
            Bucket=self._bucket_name,
            Key=key,
        )
    )

  async def document_exists(self, key: str) -> bool:
    """Check if document exists in S3."""
    try:
      await self._run_in_executor(
          lambda: self._s3_client.head_object(
              Bucket=self._bucket_name,
              Key=key,
          )
      )
      return True
    except ClientError as e:
      # Check if it's a 404 error (object not found)
      error_code = e.response.get("Error", {}).get("Code")
      if error_code == "404":
        return False
      # For other errors, log and return False
      logger.error(f"Error checking if document exists: {e}")
      return False

  async def delete_folder(self, prefix: str) -> int:
    """Delete all objects with the given prefix (folder) and return count of deleted objects."""
    deleted_count = 0

    # List all objects with the prefix
    paginator = self._s3_client.get_paginator("list_objects_v2")
    pages = paginator.paginate(Bucket=self._bucket_name, Prefix=prefix)

    for page in pages:
      objects = page.get("Contents", [])
      if not objects:
        continue

      # Prepare objects for batch deletion
      delete_objects = [{"Key": obj["Key"]} for obj in objects]

      # Delete objects in batch
      def _delete_batch():
        self._s3_client.delete_objects(
            Bucket=self._bucket_name,
            Delete={"Objects": delete_objects},
        )

      await self._run_in_executor(_delete_batch)
      deleted_count += len(delete_objects)

    return deleted_count

  async def _run_in_executor(self, func):
    """Run a blocking function in the thread pool executor."""
    return await asyncio.get_event_loop().run_in_executor(executor, func)
