"""Clients are shared per destination and never across destinations, so a second bucket, region or
profile is never served by the first one's client."""

import threading

from devkit.aws import DynamoClient, S3Client, reset_clients

from .helpers import run


def test_one_dynamodb_client_per_destination(monkeypatch):
  # AWS_DEFAULT_REGION outranks a profile's region; without it each profile's own applies.
  monkeypatch.delenv("AWS_DEFAULT_REGION")
  default = DynamoClient()
  assert DynamoClient().client is default.client
  others = [
      DynamoClient(region_name="eu-west-1"),
      DynamoClient(endpoint_url="http://127.0.0.1:9"),
      DynamoClient(profile_name="alpha"),
      DynamoClient(profile_name="beta"),
  ]
  clients = {id(default.client)} | {id(c.client) for c in others}
  assert len(clients) == 5
  assert default.client.meta.region_name == "us-east-1"
  assert DynamoClient(profile_name="alpha").client.meta.region_name == "eu-west-1"
  assert DynamoClient(profile_name="beta").client.meta.region_name == "us-west-2"


def test_s3_clients_share_a_client_and_keep_their_own_bucket():
  a, b = S3Client("bucket-a"), S3Client("bucket-b")
  assert a.client is b.client
  assert (a.bucket_name, b.bucket_name) == ("bucket-a", "bucket-b")
  assert S3Client("bucket-a", region_name="eu-west-1").client is not a.client


def test_profiles_sign_with_their_own_credentials_and_region(monkeypatch):
  monkeypatch.delenv("AWS_DEFAULT_REGION")
  alpha = run(S3Client("bucket-a", profile_name="alpha").presigned_get_url("k", expires_in=60))
  default = run(S3Client("bucket-a").presigned_get_url("k", expires_in=60))
  assert "AKIAALPHAFAKE" in alpha and "eu-west-1" in alpha
  assert "AKIADEFAULTFAKE" in default and "us-east-1" in default


def test_resources_belong_to_one_thread():
  dynamo = DynamoClient()
  here = dynamo.get_table("t").meta.client
  assert dynamo.get_table("t").meta.client is here
  elsewhere = []
  worker = threading.Thread(target=lambda: elsewhere.append(dynamo.get_table("t").meta.client))
  worker.start()
  worker.join()
  assert elsewhere[0] is not here


def test_reset_clients_makes_new_ones():
  dynamo = DynamoClient()
  client, table = dynamo.client, dynamo.get_table("t").meta.client
  reset_clients()
  assert dynamo.client is not client
  assert dynamo.get_table("t").meta.client is not table


def test_repr_names_the_destination_and_no_secret():
  shown = repr(DynamoClient(profile_name="alpha")) + repr(S3Client("bucket-a", profile_name="alpha"))
  assert "alpha" in shown
  assert "secret" not in shown and "AKIA" not in shown
