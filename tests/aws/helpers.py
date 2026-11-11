"""Helpers the AWS tests share: running a coroutine, and reading DynamoDB past DevKit."""

import asyncio
import os
import threading
import urllib.error
import urllib.request
from typing import Any

import boto3

from devkit.aws import DynamoConfig

LOCAL = os.environ.get("DEVKIT_TEST_DYNAMODB_LOCAL")


def run(coro):
  return asyncio.run(coro)


def local_answers() -> bool:
  if not LOCAL:
    return False
  try:
    urllib.request.urlopen(LOCAL, timeout=0.5)
  except urllib.error.HTTPError:
    return True
  except Exception:
    return False
  return True


def create_table(spec: DynamoConfig) -> None:
  """The table `spec` names, made through boto3 directly, as a deployment would."""
  client = boto3.client("dynamodb")
  keys = [(spec.pk_field, "HASH")] + ([(spec.sk_field, "RANGE")] if spec.sk_field else [])
  client.create_table(
      TableName=spec.table_name,
      BillingMode="PAY_PER_REQUEST",
      AttributeDefinitions=[{"AttributeName": name, "AttributeType": "S"} for name, _ in keys],
      KeySchema=[{"AttributeName": name, "KeyType": kind} for name, kind in keys],
  )


def raw_item(spec: DynamoConfig, key: dict) -> Any:
  """The item as DynamoDB holds it, read past every DevKit layer."""
  table = boto3.resource("dynamodb").Table(spec.table_name)  # pyright: ignore[reportAttributeAccessIssue]
  return table.get_item(Key=key, ConsistentRead=True).get("Item")


def raw_put(spec: DynamoConfig, item: dict) -> None:
  table = boto3.resource("dynamodb").Table(spec.table_name)  # pyright: ignore[reportAttributeAccessIssue]
  table.put_item(Item=item)


class Recorder:
  """Wraps a low-level client method: records each call's parameters and thread, and runs the
  failures queued in `script` before delegating."""

  def __init__(self, client, operation: str):
    self.calls: list[dict] = []
    self.threads: list[int] = []
    self.script: list = []
    self._real = getattr(client, operation)

  def __call__(self, *args, **params):
    self.calls.append(params)
    self.threads.append(threading.get_ident())
    if self.script:
      step = self.script.pop(0)
      if step is not None:
        return step(self._real, params)
    return self._real(*args, **params)
