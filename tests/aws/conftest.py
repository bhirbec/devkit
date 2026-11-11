"""Fixtures for the AWS helpers: fake credentials and config files, so no test can reach a real
account, moto in-process, and DynamoDB Local where DEVKIT_TEST_DYNAMODB_LOCAL names one, e.g.

  docker run -d --rm -p 127.0.0.1:8799:8000 amazon/dynamodb-local:2.5.2 -jar DynamoDBLocal.jar -inMemory -sharedDb
  DEVKIT_TEST_DYNAMODB_LOCAL=http://127.0.0.1:8799 uv run pytest tests/aws

Tests that race real threads run on DynamoDB Local only, where commits are atomic across threads:
moto applies a transaction's checks and writes without excluding another thread's.
"""

import os
import uuid

import boto3
import pytest
from moto import mock_aws

from devkit.aws import DynamoConfig, reset_clients

from .helpers import LOCAL, Recorder, create_table, local_answers

AWS_CONFIG = """
[default]
region = us-east-1

[profile alpha]
region = eu-west-1

[profile beta]
region = us-west-2
"""

AWS_CREDENTIALS = """
[default]
aws_access_key_id = AKIADEFAULTFAKE
aws_secret_access_key = default-secret-not-real

[alpha]
aws_access_key_id = AKIAALPHAFAKE
aws_secret_access_key = alpha-secret-not-real

[beta]
aws_access_key_id = AKIABETAFAKE
aws_secret_access_key = beta-secret-not-real
"""


@pytest.fixture(autouse=True)
def aws_environment(monkeypatch, tmp_path):
  for name in list(os.environ):
    if name.startswith("AWS_"):
      monkeypatch.delenv(name)
  config, credentials = tmp_path / "aws-config", tmp_path / "aws-credentials"
  config.write_text(AWS_CONFIG)
  credentials.write_text(AWS_CREDENTIALS)
  monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
  monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(credentials))
  monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
  monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
  # The helpers below use boto3's default session: start each test with a new one too.
  monkeypatch.setattr(boto3, "DEFAULT_SESSION", None)
  reset_clients()
  yield
  reset_clients()


@pytest.fixture(params=["moto", "local"])
def backend(request, monkeypatch):
  """Where DynamoDB is: moto in-process, or DynamoDB Local. DynamoClient() reaches either."""
  if request.param == "moto":
    with mock_aws():
      yield "moto"
  else:
    if not local_answers():
      pytest.skip("DynamoDB Local is not running at DEVKIT_TEST_DYNAMODB_LOCAL")
    monkeypatch.setenv("AWS_ENDPOINT_URL_DYNAMODB", LOCAL)
    reset_clients()
    yield "local"


@pytest.fixture
def table(backend):
  """A fresh table with a partition and a sort key, named uniquely: DynamoDB Local keeps tables."""
  spec = DynamoConfig(f"devkit-test-{uuid.uuid4().hex[:12]}", pk_field="pk", sk_field="sk")
  create_table(spec)
  yield spec
  boto3.client("dynamodb").delete_table(TableName=spec.table_name)


@pytest.fixture
def record(monkeypatch):
  def wrap(client, operation: str) -> Recorder:
    recorder = Recorder(client, operation)
    monkeypatch.setattr(client, operation, recorder)
    return recorder

  return wrap
