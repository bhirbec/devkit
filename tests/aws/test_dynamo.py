"""The original DynamoDB API keeps working as its callers use it (prdhub's models), with the gaps
closed: conditions with their own placeholders, consistent reads, every page on request, builder
transactions sent to the client's own destination with correct types."""

import threading
import uuid
from decimal import Decimal
from typing import ClassVar, Dict, List

import boto3
import pytest
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from moto import mock_aws
from pydantic import BaseModel, ConfigDict

from devkit.aws import DynamoClient, DynamoConfig, DynamoModel, DynamoTransaction

from .helpers import create_table, raw_item, raw_put, run


class Settings(BaseModel):
  plan: str = "free"
  quota: Decimal = Decimal("1.5")


class Workspace(DynamoModel):
  model_config = ConfigDict(extra="forbid")

  workspace_id: str
  owner: str
  name: str
  active: bool = True
  settings: Settings = Settings()
  tags: List[str] = []
  permissions: Dict[str, str] = {}
  notes: str = ""

  _dynamo_config: ClassVar[DynamoConfig] = DynamoConfig(
      table_name="workspaces", pk_field="workspace_id", relations=["notes"]
  )


class Doc(DynamoModel):
  workspace_id: str
  doc_id: str
  title: str = ""

  _dynamo_config: ClassVar[DynamoConfig] = DynamoConfig(
      table_name="documents", pk_field="workspace_id", sk_field="doc_id"
  )


@pytest.fixture
def tables():
  with mock_aws():
    create_table(Workspace._dynamo_config)
    create_table(Doc._dynamo_config)
    yield


def test_model_crud_round_trips_like_before(tables):
  ws = Workspace(workspace_id="ws1", owner="u1", name="Default", notes="kept elsewhere")
  run(ws.put("attribute_not_exists(workspace_id)"))
  assert "notes" not in raw_item(Workspace._dynamo_config, {"workspace_id": "ws1"})

  loaded = run(Workspace.get("ws1"))
  assert loaded is not None
  assert (loaded.name, loaded.active, loaded.settings.quota) == ("Default", True, Decimal("1.5"))

  loaded.name = "Renamed"
  assert loaded.get_dirty_fields() == {"name"}
  run(loaded.update())
  assert loaded.get_dirty_fields() == set()
  renamed = run(Workspace.get("ws1", consistent_read=True))
  assert renamed is not None and renamed.name == "Renamed"

  run(loaded.delete())
  assert run(Workspace.get("ws1")) is None


def test_put_condition_takes_its_own_placeholders(tables):
  ws = Workspace(workspace_id="ws1", owner="u1", name="Default")
  run(ws.put())
  same_owner = dict(
      expression_attribute_names={"#o": "owner"}, expression_attribute_values={":o": "u1"}
  )
  run(ws.put("attribute_not_exists(workspace_id) OR #o = :o", **same_owner))
  with pytest.raises(ClientError) as raised:
    run(ws.put("#o = :o", expression_attribute_names={"#o": "owner"},
               expression_attribute_values={":o": "someone else"}))
  assert raised.value.response["Error"]["Code"] == "ConditionalCheckFailedException"


def test_update_placeholders_leave_the_callers_free(tables):
  ws = Workspace(workspace_id="ws1", owner="u1", name="Default")
  run(ws.put())
  ws.name = "Renamed"
  # :v0 and #k0 were the generated names once; a condition may use them now.
  run(ws.update("#k0 = :v0", expression_attribute_names={"#k0": "owner"},
                expression_attribute_values={":v0": "u1"}))
  assert raw_item(Workspace._dynamo_config, {"workspace_id": "ws1"})["name"] == "Renamed"

  ws.name = "Again"
  with pytest.raises(ValueError):
    run(ws.update("#upd0 = :x", expression_attribute_names={"#upd0": "owner"},
                  expression_attribute_values={":x": "u1"}))


def test_placeholders_without_a_condition_are_refused(tables):
  with pytest.raises(ValueError):
    run(DynamoClient().put("workspaces", {"workspace_id": "x"},
                           expression_attribute_values={":v": 1}))


def test_models_load_items_a_transaction_versioned(tables):
  raw_put(Workspace._dynamo_config, {
      "workspace_id": "ws1", "owner": "u1", "name": "n", "created_at": "t", "version": 3,
  })
  loaded = run(Workspace.get("ws1"))
  assert loaded is not None and loaded.name == "n"
  assert [w.workspace_id for w in run(Workspace.query(
      KeyConditionExpression=Key("workspace_id").eq("ws1")))] == ["ws1"]


def test_query_reads_one_page_and_query_all_reads_them_all(tables):
  for i in range(25):
    raw_put(Doc._dynamo_config, {"workspace_id": "ws1", "doc_id": f"d{i:02d}", "created_at": "t"})
  raw_put(Doc._dynamo_config, {"workspace_id": "ws2", "doc_id": "other", "created_at": "t"})
  condition = Key("workspace_id").eq("ws1")

  assert len(run(Doc.query(KeyConditionExpression=condition, Limit=10))) == 10
  every = run(Doc.query_all(KeyConditionExpression=condition, Limit=10))
  assert [d.doc_id for d in every] == [f"d{i:02d}" for i in range(25)]
  assert len(run(Doc.query_all(KeyConditionExpression=condition, Limit=10, max_items=12))) == 12

  page = run(DynamoClient().query("documents", KeyConditionExpression=condition, Limit=10))
  assert len(page["Items"]) == 10 and "LastEvaluatedKey" in page


def test_consistent_read_reaches_dynamodb(tables, monkeypatch):
  sent = []

  class Table:
    def get_item(self, **params):
      sent.append(params)
      return {}

  monkeypatch.setattr(DynamoClient, "get_table", lambda self, name: Table())
  run(Workspace.get("ws1", consistent_read=True))
  run(Workspace.get("ws1"))
  assert sent == [{"Key": {"workspace_id": "ws1"}, "ConsistentRead": True},
                  {"Key": {"workspace_id": "ws1"}}]


def test_batch_delete_partition_deletes_only_that_partition(tables):
  for i in range(30):
    raw_put(Doc._dynamo_config, {"workspace_id": "ws1", "doc_id": f"d{i}", "created_at": "t"})
  raw_put(Doc._dynamo_config, {"workspace_id": "ws2", "doc_id": "keep", "created_at": "t"})
  assert run(Doc.batch_delete_partition("ws1", page_limit=7))["deleted_count"] == 30
  assert run(Doc.query_all(KeyConditionExpression=Key("workspace_id").eq("ws1"))) == []
  assert raw_item(Doc._dynamo_config, {"workspace_id": "ws2", "doc_id": "keep"}) is not None


def test_builder_transaction_commits_booleans_decimals_and_nested_models(tables):
  ws = Workspace(workspace_id="ws1", owner="u1", name="Default", tags=["a"],
                 permissions={"ws1": "admin"})
  doc = Doc(workspace_id="ws1", doc_id="home", title="Home")

  async def onboard():
    async with DynamoClient() as dynamodb:
      async with dynamodb.transaction() as transaction:
        transaction.put(ws, "attribute_not_exists(workspace_id)")
        transaction.put(doc, "attribute_not_exists(doc_id)")

  run(onboard())
  stored = raw_item(Workspace._dynamo_config, {"workspace_id": "ws1"})
  assert stored["active"] is True
  assert stored["settings"] == {"plan": "free", "quota": Decimal("1.5")}
  home = run(Doc.get("ws1", "home"))
  assert home is not None and home.title == "Home"


def test_builder_transaction_conditions_take_placeholders_and_raise_as_before(tables):
  ws = Workspace(workspace_id="ws1", owner="u1", name="Default")
  run(ws.put())
  ws.name = "Renamed"

  async def guarded(owner):
    async with DynamoClient().transaction() as transaction:
      transaction.update(ws, "#o = :o", expression_attribute_names={"#o": "owner"},
                         expression_attribute_values={":o": owner})
      transaction.condition_check(Doc(workspace_id="ws1", doc_id="home"),
                                  "attribute_not_exists(doc_id)")

  with pytest.raises(ClientError) as raised:
    run(guarded("someone else"))
  assert raised.value.response["Error"]["Code"] == "TransactionCanceledException"
  assert ws.get_dirty_fields() == {"name"}

  run(guarded("u1"))
  assert ws.get_dirty_fields() == set()
  assert raw_item(Workspace._dynamo_config, {"workspace_id": "ws1"})["name"] == "Renamed"


def test_builder_transaction_goes_to_the_clients_destination():
  with mock_aws():
    spec = DynamoConfig(f"regional-{uuid.uuid4().hex[:8]}", pk_field="workspace_id")
    boto3.client("dynamodb", region_name="eu-west-1").create_table(
        TableName=spec.table_name,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "workspace_id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "workspace_id", "KeyType": "HASH"}],
    )

    class Regional(DynamoModel):
      workspace_id: str
      _dynamo_config: ClassVar[DynamoConfig] = spec

    async def write():
      async with DynamoClient(region_name="eu-west-1").transaction() as transaction:
        transaction.put(Regional(workspace_id="w"))

    # The table exists in eu-west-1 only: a client on the default region would not find it.
    run(write())
    found = boto3.client("dynamodb", region_name="eu-west-1").get_item(
        TableName=spec.table_name, Key={"workspace_id": {"S": "w"}})
    assert "Item" in found


def test_builder_refuses_more_than_100_statements(tables):
  async def too_many():
    async with DynamoClient().transaction() as transaction:
      for i in range(101):
        transaction.put(Doc(workspace_id="ws1", doc_id=str(i)))

  with pytest.raises(ValueError):
    run(too_many())


def test_to_dynamodb_value_types_every_python_value():
  convert = DynamoTransaction(DynamoClient()).to_dynamodb_value
  assert convert(True) == {"BOOL": True}
  assert convert(3) == {"N": "3"}
  assert convert(0.1) == {"N": "0.1"}
  assert convert(Decimal("2.50")) == {"N": "2.50"}
  assert convert(None) == {"NULL": True}
  assert convert(b"\x00") == {"B": b"\x00"}
  assert convert({"a": [1, "x"]}) == {"M": {"a": {"L": [{"N": "1"}, {"S": "x"}]}}}
  assert convert({"x", "y"})["SS"] in (["x", "y"], ["y", "x"])


def test_blocking_calls_run_off_the_event_loop(tables, monkeypatch):
  threads = []
  real = DynamoClient.get_table

  def recording(self, name):
    threads.append(threading.get_ident())
    return real(self, name)

  monkeypatch.setattr(DynamoClient, "get_table", recording)
  loop_thread = []

  async def calls():
    loop_thread.append(threading.get_ident())
    ws = Workspace(workspace_id="ws1", owner="u1", name="n")
    await ws.put()
    await Workspace.get("ws1")
    await Workspace.query_all(KeyConditionExpression=Key("workspace_id").eq("ws1"))
    await Doc.batch_delete_partition("ws1")

  run(calls())
  assert threads and loop_thread[0] not in threads
