"""S3Client on moto: one bucket per client, bytes and text, SHA-256 integrity, presigned downloads,
not-found told apart from refusals, and no blocking call on the event loop."""

import base64
import hashlib
import threading
from urllib.parse import parse_qs, urlparse

import boto3
import pytest
from botocore.exceptions import ClientError, FlexibleChecksumError
from moto import mock_aws

from devkit.aws import S3Client, S3DeleteError, S3IntegrityError, S3ObjectNotFoundError

from .helpers import run

DATA = bytes(range(256)) * 3 + b"\xff\xfe\x00"


@pytest.fixture
def s3():
  with mock_aws():
    client = boto3.client("s3")
    for name in ("bucket-a", "bucket-b"):
      client.create_bucket(Bucket=name)
    yield client


def keys(s3, bucket):
  return sorted(o["Key"] for o in s3.list_objects_v2(Bucket=bucket).get("Contents", []))


def test_each_client_writes_to_its_own_bucket(s3):
  a, b = S3Client("bucket-a"), S3Client("bucket-b")
  run(a.upload_document("k", "from a"))
  run(b.upload_document("k", "from b"))
  run(S3Client("bucket-a").put_bytes("only-a", b"x"))
  assert run(a.download_document("k")) == "from a"
  assert run(b.download_document("k")) == "from b"
  assert keys(s3, "bucket-a") == ["k", "only-a"]
  assert keys(s3, "bucket-b") == ["k"]


def test_bytes_round_trip_with_their_sha256(s3, record):
  bucket = S3Client("bucket-a")
  puts = record(bucket.client, "put_object")
  digest = hashlib.sha256(DATA).hexdigest()

  result = run(bucket.put_bytes("blobs/1", DATA, content_type="application/pdf",
                                metadata={"owner": "u1"}))
  assert (result.key, result.sha256) == ("blobs/1", digest)
  sent = puts.calls[0]
  assert sent["Body"] == DATA
  assert sent["ChecksumSHA256"] == base64.b64encode(hashlib.sha256(DATA).digest()).decode()

  assert run(bucket.get_bytes("blobs/1")) == DATA
  assert run(bucket.get_bytes("blobs/1", expected_sha256=digest)) == DATA
  with pytest.raises(S3IntegrityError):
    run(bucket.get_bytes("blobs/1", expected_sha256="0" * 64))
  info = run(bucket.head_object("blobs/1"))
  assert info is not None
  assert (info.size, info.content_type, info.metadata) == (len(DATA), "application/pdf", {"owner": "u1"})


def test_a_sha256_that_does_not_match_is_refused_before_upload(s3, record):
  bucket = S3Client("bucket-a")
  puts = record(bucket.client, "put_object")
  with pytest.raises(S3IntegrityError):
    run(bucket.put_bytes("k", DATA, sha256=hashlib.sha256(b"other").hexdigest()))
  with pytest.raises(ValueError):
    run(bucket.put_bytes("k", DATA, sha256="NOT-HEX"))
  assert puts.calls == []
  assert run(bucket.object_exists("k")) is False


def test_a_checksum_mismatch_while_reading_is_an_integrity_error(s3, monkeypatch):
  bucket = S3Client("bucket-a")

  class Corrupted:
    def read(self):
      raise FlexibleChecksumError(error_msg="Expected checksum A did not match calculated checksum B")

  monkeypatch.setattr(bucket.client, "get_object", lambda **params: {"Body": Corrupted()})
  with pytest.raises(S3IntegrityError):
    run(bucket.get_bytes("k"))


def test_reads_ask_s3_to_check_its_stored_checksum(s3, record):
  bucket = S3Client("bucket-a")
  run(bucket.put_bytes("k", DATA))
  gets, heads = record(bucket.client, "get_object"), record(bucket.client, "head_object")
  run(bucket.get_bytes("k"))
  run(bucket.head_object("k"))
  assert gets.calls[0]["ChecksumMode"] == heads.calls[0]["ChecksumMode"] == "ENABLED"


def test_text_documents_keep_their_api(s3):
  bucket = S3Client("bucket-a")
  assert run(bucket.upload_document("docs/a.md", "# Ünïcode", "text/markdown")) == "s3://bucket-a/docs/a.md"
  assert run(bucket.download_document("docs/a.md")) == "# Ünïcode"
  assert run(bucket.document_exists("docs/a.md")) is True
  run(bucket.delete_document("docs/a.md"))
  run(bucket.delete_document("docs/a.md"))
  assert run(bucket.document_exists("docs/a.md")) is False


def test_a_missing_key_is_not_found_and_other_failures_are_raised(s3, monkeypatch):
  bucket = S3Client("bucket-a")
  with pytest.raises(S3ObjectNotFoundError) as raised:
    run(bucket.get_bytes("missing"))
  # Still the ClientError old callers catch, with S3's own code.
  assert isinstance(raised.value, ClientError)
  assert raised.value.response["Error"]["Code"] == "NoSuchKey"
  with pytest.raises(S3ObjectNotFoundError):
    run(bucket.download_document("missing"))
  assert run(bucket.head_object("missing")) is None
  assert run(bucket.document_exists("missing")) is False

  with pytest.raises(ClientError) as raised:
    run(S3Client("no-such-bucket").get_bytes("k"))
  assert not isinstance(raised.value, S3ObjectNotFoundError)

  def forbidden(**params):
    raise ClientError(
        {"Error": {"Code": "403", "Message": "Forbidden"}, "ResponseMetadata": {"HTTPStatusCode": 403}},
        "HeadObject",
    )

  monkeypatch.setattr(bucket.client, "head_object", forbidden)
  with pytest.raises(ClientError):
    run(bucket.document_exists("k"))


def test_listing_reads_every_page(s3):
  for i in range(12):
    s3.put_object(Bucket="bucket-a", Key=f"p/{i:02d}", Body=b"x")
  s3.put_object(Bucket="bucket-a", Key="q/other", Body=b"x")
  bucket = S3Client("bucket-a")

  async def listed():
    return [o.key async for o in bucket.iter_objects("p/", page_size=5)]

  assert run(listed()) == [f"p/{i:02d}" for i in range(12)]


def test_delete_folder_deletes_every_page_of_a_prefix(s3):
  for i in range(1003):
    s3.put_object(Bucket="bucket-a", Key=f"p/{i:04d}", Body=b"x")
  s3.put_object(Bucket="bucket-a", Key="keep/x", Body=b"x")
  assert run(S3Client("bucket-a").delete_folder("p/")) == 1003
  assert keys(s3, "bucket-a") == ["keep/x"]


def test_delete_folder_raises_when_s3_refuses_keys(s3, monkeypatch):
  for i in range(3):
    s3.put_object(Bucket="bucket-a", Key=f"p/{i}", Body=b"x")
  bucket = S3Client("bucket-a")
  refusal = {"Key": "p/1", "Code": "AccessDenied", "Message": "Access Denied"}
  monkeypatch.setattr(bucket.client, "delete_objects", lambda **params: {"Errors": [refusal]})
  with pytest.raises(S3DeleteError) as raised:
    run(bucket.delete_folder("p/"))
  assert (raised.value.deleted_count, raised.value.errors) == (2, [refusal])


def test_presigned_urls_are_sigv4_with_their_download_parameters():
  bucket = S3Client("bucket-a", region_name="eu-west-1")
  url = run(bucket.presigned_get_url(
      "reports/r 1.pdf", expires_in=300, filename='Résumé "final".pdf', content_type="application/pdf"
  ))
  parsed, query = urlparse(url), parse_qs(urlparse(url).query)
  assert parsed.path.endswith("/reports/r%201.pdf")
  assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
  assert query["X-Amz-Expires"] == ["300"]
  assert "/eu-west-1/s3/aws4_request" in query["X-Amz-Credential"][0]
  assert query["response-content-type"] == ["application/pdf"]
  assert query["response-content-disposition"] == [
      "attachment; filename=\"R_sum_ _final_.pdf\"; filename*=UTF-8''R%C3%A9sum%C3%A9%20%22final%22.pdf"
  ]
  assert "secret" not in url
  for seconds in (0, 7 * 24 * 3600 + 1):
    with pytest.raises(ValueError):
      run(bucket.presigned_get_url("k", expires_in=seconds))


def test_blocking_calls_run_off_the_event_loop(s3, record):
  bucket = S3Client("bucket-a")
  operations = ["put_object", "get_object", "head_object", "list_objects_v2", "delete_objects",
                "generate_presigned_url"]
  recorders = {op: record(bucket.client, op) for op in operations}
  body_reads = []

  def spy_on_body(real, params):
    response = real(**params)
    stream = response["Body"]

    class Spy:
      def read(self, *args):
        body_reads.append(threading.get_ident())
        return stream.read(*args)

    response["Body"] = Spy()
    return response

  recorders["get_object"].script = [spy_on_body]

  async def calls():
    await bucket.put_bytes("p/k", DATA)
    assert await bucket.get_bytes("p/k") == DATA
    await bucket.object_exists("p/k")
    await bucket.presigned_get_url("p/k", expires_in=60)
    await bucket.delete_folder("p/")
    return threading.get_ident()

  loop_thread = run(calls())
  for op, recorder in recorders.items():
    assert recorder.threads and loop_thread not in recorder.threads, op
  assert body_reads and loop_thread not in body_reads
