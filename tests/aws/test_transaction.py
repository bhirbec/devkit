"""run_transaction's contract on moto and DynamoDB Local (conftest.py): consistent reads, version and
absence guards, read-only checks, reruns on a lost race, resends of a lost commit with its own token,
and the failures that are raised instead of retried."""

import asyncio
import threading
from decimal import Decimal

import pytest
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError, ReadTimeoutError
from pydantic import ConfigDict

from devkit.aws import (Condition, ConditionFailedError, DynamoClient, DynamoModel,
                        TransactionCanceledError, TransactionConflictError,
                        TransactionOutcomeUnknownError, TransactionUsageError)
from devkit.aws import transaction as tx_module

from .helpers import local_answers, raw_item, raw_put, run


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
  slept = []

  async def sleep(seconds):
    slept.append(seconds)

  monkeypatch.setattr(tx_module, "_sleep", sleep)
  return slept


def key(sk="x", pk="p"):
  return {"pk": pk, "sk": sk}


def cancellation(*codes, items=None):
  """A TransactionCanceledException as botocore raises it, one reason per action."""
  reasons = []
  for i, code in enumerate(codes):
    reason = {"Code": code}
    if items and items[i] is not None:
      reason["Item"] = items[i]
    reasons.append(reason)
  return ClientError(
      {
          "Error": {"Code": "TransactionCanceledException", "Message": "canceled"},
          "CancellationReasons": reasons,
          "ResponseMetadata": {"HTTPStatusCode": 400},
      },
      "TransactWriteItems",
  )


def service_error(code, status=400):
  return ClientError(
      {"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
      "TransactWriteItems",
  )


def raise_(error):
  def step(real, params):
    raise error

  return step


def lose_response(real, params):
  """The commit lands, and its answer is lost on the way back."""
  real(**params)
  raise ReadTimeoutError(endpoint_url="https://dynamodb.test")


async def increment(tx, spec, item_key=None, by=1):
  item = await tx.get(spec, item_key or key())
  if item is None:
    tx.insert(spec, {**(item_key or key()), "n": by})
  else:
    tx.put(spec, {**item, "n": item["n"] + by})


def test_commit_writes_every_item_once_with_versions(table):
  dynamo = DynamoClient()

  async def open_accounts(tx):
    assert await tx.get(table, key("a")) is None
    tx.insert(table, {**key("a"), "balance": 10})
    tx.insert(table, {**key("b"), "balance": 0, "version": 99})
    return "opened"

  async def transfer(tx):
    a, b = await tx.get(table, key("a")), await tx.get(table, key("b"))
    tx.put(table, {**a, "balance": a["balance"] - 4})
    tx.put(table, {**b, "balance": b["balance"] + 4})

  assert run(dynamo.run_transaction(open_accounts)) == "opened"
  run(dynamo.run_transaction(transfer))
  assert raw_item(table, key("a")) == {**key("a"), "balance": 6, "version": 2}
  # A version given by the caller is ignored: the transaction owns it.
  assert raw_item(table, key("b")) == {**key("b"), "balance": 4, "version": 2}


def test_reads_see_the_transactions_own_writes(table):
  dynamo = DynamoClient()
  seen = []

  async def fn(tx):
    tx.insert(table, {**key(), "n": 1})
    seen.append(await tx.get(table, key()))
    tx.delete(table, key())
    seen.append(await tx.get(table, key()))

  run(dynamo.run_transaction(fn))
  assert seen == [{**key(), "n": 1}, None]
  assert raw_item(table, key()) is None


def test_a_competing_update_reruns_on_fresh_state_and_loses_nothing(table, record):
  dynamo = DynamoClient()
  raw_put(table, {**key(), "n": 0, "version": 1})
  seen = []

  async def scenario():
    a_read, b_done = asyncio.Event(), asyncio.Event()

    async def slow_increment(tx):
      item = await tx.get(table, key())
      seen.append(item["n"])
      if len(seen) == 1:
        a_read.set()
        await b_done.wait()
      tx.put(table, {**item, "n": item["n"] + 1})

    first = asyncio.create_task(dynamo.run_transaction(slow_increment))
    await a_read.wait()
    await dynamo.run_transaction(lambda tx: increment(tx, table))
    b_done.set()
    await first

  sends = record(dynamo.client, "transact_write_items")
  run(scenario())
  assert seen == [0, 1]
  assert raw_item(table, key()) == {**key(), "n": 2, "version": 3}
  # Three commits were sent; the lost one used its own token and was never resent.
  assert len({c["ClientRequestToken"] for c in sends.calls}) == 3


def test_an_absent_record_race_inserts_once(table):
  dynamo = DynamoClient()
  results = []

  async def scenario():
    a_read, b_done = asyncio.Event(), asyncio.Event()

    async def create_if_absent(tx, owner, wait):
      found = await tx.get(table, key())
      if wait and not a_read.is_set():
        a_read.set()
        await b_done.wait()
      if found is not None:
        return f"{owner} found {found['owner']}"
      tx.insert(table, {**key(), "owner": owner})
      return f"{owner} created"

    first = asyncio.create_task(dynamo.run_transaction(lambda tx: create_if_absent(tx, "a", True)))
    await a_read.wait()
    results.append(await dynamo.run_transaction(lambda tx: create_if_absent(tx, "b", False)))
    b_done.set()
    results.append(await first)

  run(scenario())
  assert results == ["b created", "a found b"]
  assert raw_item(table, key()) == {**key(), "owner": "b", "version": 1}


def test_a_blind_insert_over_an_item_conflicts_until_its_attempts_run_out(table, record):
  dynamo = DynamoClient()
  raw_put(table, {**key(), "owner": "someone"})
  sends = record(dynamo.client, "transact_write_items")

  async def insert(tx):
    tx.insert(table, {**key(), "owner": "me"})

  with pytest.raises(TransactionConflictError) as raised:
    run(dynamo.run_transaction(insert, max_attempts=2))
  assert raised.value.attempts == 2
  assert raised.value.reasons[0].item == {**key(), "owner": "someone"}
  assert len(sends.calls) == 2
  assert raw_item(table, key()) == {**key(), "owner": "someone"}


def test_a_read_only_guard_reruns_when_the_item_read_changes(table, record):
  dynamo = DynamoClient()
  raw_put(table, {**key("limit"), "max": 1, "version": 1})
  raw_put(table, {**key("count"), "n": 0, "version": 1})
  outcomes = []

  async def scenario():
    a_read, b_done = asyncio.Event(), asyncio.Event()

    async def take_if_allowed(tx):
      limit, count = await tx.get(table, key("limit")), await tx.get(table, key("count"))
      if not a_read.is_set():
        a_read.set()
        await b_done.wait()
      if count["n"] >= limit["max"]:
        outcomes.append("refused")
        return
      tx.put(table, {**count, "n": count["n"] + 1})
      outcomes.append("taken")

    async def lower_limit(tx):
      limit = await tx.get(table, key("limit"))
      tx.put(table, {**limit, "max": 0})

    first = asyncio.create_task(dynamo.run_transaction(take_if_allowed))
    await a_read.wait()
    await dynamo.run_transaction(lower_limit)
    b_done.set()
    await first

  sends = record(dynamo.client, "transact_write_items")
  run(scenario())
  assert outcomes == ["taken", "refused"]
  assert raw_item(table, key("count")) == {**key("count"), "n": 0, "version": 1}
  first_commit = sends.calls[1]["TransactItems"]
  assert [next(iter(action)) for action in first_commit] == ["ConditionCheck", "Put"]
  check = first_commit[0]["ConditionCheck"]
  assert check["ConditionExpression"] == "#dkver = :dkver"
  assert check["ExpressionAttributeValues"] == {":dkver": {"N": "1"}}


def test_conflicts_stop_after_max_attempts_and_write_nothing(table):
  dynamo = DynamoClient()
  raw_put(table, {**key(), "n": 0, "version": 1})
  calls = []

  async def always_overtaken(tx):
    item = await tx.get(table, key())
    calls.append(item["version"])
    # Another writer commits between this read and the commit, every time.
    raw_put(table, {**item, "version": item["version"] + 1})
    tx.put(table, {**item, "n": 100})

  with pytest.raises(TransactionConflictError) as raised:
    run(dynamo.run_transaction(always_overtaken, max_attempts=3))
  assert calls == [1, 2, 3]
  assert raised.value.attempts == 3
  assert [r.code for r in raised.value.reasons] == ["ConditionalCheckFailed"]
  assert raw_item(table, key())["n"] == 0


def test_a_failed_condition_on_unchanged_reads_is_terminal(table):
  dynamo = DynamoClient()
  raw_put(table, {**key(), "balance": 5, "version": 1})
  calls = []

  async def debit(tx):
    item = await tx.get(table, key())
    calls.append(1)
    tx.put(
        table,
        {**item, "balance": item["balance"] - 10},
        condition=Condition("#b >= :amount", {"#b": "balance"}, {":amount": 10}),
    )

  with pytest.raises(ConditionFailedError) as raised:
    run(dynamo.run_transaction(debit))
  assert calls == [1]
  reason = raised.value.reasons[0]
  assert reason.code == "ConditionalCheckFailed"
  assert reason.item is not None and reason.item["balance"] == 5
  assert raw_item(table, key())["balance"] == 5


def test_a_check_condition_fails_without_a_write(table):
  dynamo = DynamoClient()
  raw_put(table, {**key("flag"), "enabled": False})

  async def only_if_enabled(tx):
    tx.check(table, key("flag"), Condition("#e = :yes", {"#e": "enabled"}, {":yes": True}))
    tx.insert(table, {**key("thing"), "made": True})

  with pytest.raises(ConditionFailedError):
    run(dynamo.run_transaction(only_if_enabled))
  assert raw_item(table, key("thing")) is None


def test_an_exception_from_the_function_commits_nothing_and_is_not_retried(table):
  dynamo = DynamoClient()
  calls = []

  async def refuses(tx):
    calls.append(1)
    tx.insert(table, {**key(), "n": 1})
    raise LookupError("not today")

  with pytest.raises(LookupError):
    run(dynamo.run_transaction(refuses))
  assert calls == [1]
  assert raw_item(table, key()) is None


def test_reads_alone_commit_nothing(table, record):
  dynamo = DynamoClient()
  sends = record(dynamo.client, "transact_write_items")

  async def look(tx):
    return await tx.get(table, key())

  assert run(dynamo.run_transaction(look)) is None
  assert sends.calls == []


def test_tx_reads_are_strongly_consistent_and_off_the_event_loop(table, record):
  dynamo = DynamoClient()
  gets, sends = record(dynamo.client, "get_item"), record(dynamo.client, "transact_write_items")
  loop_thread = []

  async def fn(tx):
    loop_thread.append(threading.get_ident())
    await increment(tx, table)

  run(dynamo.run_transaction(fn))
  assert gets.calls[0]["ConsistentRead"] is True
  assert loop_thread[0] not in gets.threads + sends.threads


def test_a_lost_commit_answer_is_resent_with_the_same_token_and_applies_once(table, record, backend):
  dynamo = DynamoClient()
  raw_put(table, {**key(), "n": 0, "version": 1})
  sends = record(dynamo.client, "transact_write_items")
  if backend == "moto":
    # moto ignores ClientRequestToken; this front answers a replayed token as DynamoDB does.
    sends.script = [lose_response, lambda real, params: {}]
  else:
    sends.script = [lose_response]
  calls = []

  async def counted_increment(tx):
    calls.append(1)
    await increment(tx, table)

  run(dynamo.run_transaction(counted_increment))
  assert calls == [1]
  assert len(sends.calls) == 2
  assert sends.calls[0]["ClientRequestToken"] == sends.calls[1]["ClientRequestToken"]
  assert raw_item(table, key()) == {**key(), "n": 1, "version": 2}


def test_a_transaction_in_progress_is_resent_with_its_token(table, record):
  dynamo = DynamoClient()
  sends = record(dynamo.client, "transact_write_items")
  sends.script = [raise_(service_error("TransactionInProgressException"))]
  run(dynamo.run_transaction(lambda tx: increment(tx, table)))
  assert len(sends.calls) == 2
  assert sends.calls[0]["ClientRequestToken"] == sends.calls[1]["ClientRequestToken"]
  assert raw_item(table, key())["n"] == 1


def test_an_unknown_outcome_is_raised_after_bounded_resends_and_never_rerun(table, record, backend):
  dynamo = DynamoClient()
  sends = record(dynamo.client, "transact_write_items")
  timeout = ReadTimeoutError(endpoint_url="https://dynamodb.test")
  sends.script = [raise_(timeout), raise_(service_error("InternalServerError", 500)), raise_(timeout)]
  calls = []

  async def counted_increment(tx):
    calls.append(1)
    await increment(tx, table)

  with pytest.raises(TransactionOutcomeUnknownError) as raised:
    run(dynamo.run_transaction(counted_increment, max_commit_attempts=3))
  error = raised.value
  assert calls == [1]
  assert error.attempts == 3
  assert {c["ClientRequestToken"] for c in sends.calls} == {error.client_request_token}
  assert raw_item(table, key()) is None

  # Settling it later with the same token applies it once, however many times it is sent.
  if backend == "moto":
    sends.script = [None, lambda real, params: {}]
  run(dynamo.transact_write_items(error.transact_items, client_request_token=error.client_request_token))
  run(dynamo.transact_write_items(error.transact_items, client_request_token=error.client_request_token))
  assert raw_item(table, key()) == {**key(), "n": 1, "version": 1}


def test_the_commit_deadline_stops_resending(table, record):
  dynamo = DynamoClient()
  sends = record(dynamo.client, "transact_write_items")
  sends.script = [raise_(ReadTimeoutError(endpoint_url="https://dynamodb.test"))] * 5
  with pytest.raises(TransactionOutcomeUnknownError) as raised:
    run(dynamo.run_transaction(lambda tx: increment(tx, table), commit_deadline_seconds=0))
  assert raised.value.attempts == 1
  assert len(sends.calls) == 1


@pytest.mark.parametrize("code", [
    "AccessDeniedException",
    "ValidationException",
    "IdempotentParameterMismatchException",
    "ResourceNotFoundException",
])
def test_definite_errors_are_raised_as_they_came_without_resend_or_rerun(table, record, code):
  dynamo = DynamoClient()
  sends = record(dynamo.client, "transact_write_items")
  sends.script = [raise_(service_error(code))]
  calls = []

  async def counted_increment(tx):
    calls.append(1)
    await increment(tx, table)

  with pytest.raises(ClientError) as raised:
    run(dynamo.run_transaction(counted_increment))
  assert raised.value.response["Error"]["Code"] == code
  assert (calls, len(sends.calls)) == ([1], 1)


@pytest.mark.parametrize("code", ["TransactionConflict", "ThrottlingError", "ProvisionedThroughputExceeded"])
def test_retryable_cancellations_rerun_with_a_new_token(table, record, code):
  dynamo = DynamoClient()
  sends = record(dynamo.client, "transact_write_items")
  sends.script = [raise_(cancellation(code))]
  calls = []

  async def counted_increment(tx):
    calls.append(1)
    await increment(tx, table)

  run(dynamo.run_transaction(counted_increment))
  assert calls == [1, 1]
  assert sends.calls[0]["ClientRequestToken"] != sends.calls[1]["ClientRequestToken"]
  assert raw_item(table, key())["n"] == 1


@pytest.mark.parametrize("code", ["ValidationError", "ItemCollectionSizeLimitExceeded", "SomethingNew"])
def test_other_cancellations_are_terminal(table, record, code):
  dynamo = DynamoClient()
  sends = record(dynamo.client, "transact_write_items")
  sends.script = [raise_(cancellation(code))]
  calls = []

  async def counted_increment(tx):
    calls.append(1)
    await increment(tx, table)

  with pytest.raises(TransactionCanceledError) as raised:
    run(dynamo.run_transaction(counted_increment))
  assert not isinstance(raised.value, ConditionFailedError)
  assert [r.code for r in raised.value.reasons] == [code]
  assert calls == [1]


def test_a_lost_race_outranks_a_failed_condition_in_the_same_commit(table, record):
  dynamo = DynamoClient()
  raw_put(table, {**key("a"), "n": 0, "version": 1})
  raw_put(table, {**key("b"), "n": 0, "version": 1})
  sends = record(dynamo.client, "transact_write_items")
  # a failed its condition on the version read; b moved on: the rule may have been decided from b.
  sends.script = [raise_(cancellation(
      "ConditionalCheckFailed",
      "ConditionalCheckFailed",
      items=[
          {"pk": {"S": "p"}, "sk": {"S": "a"}, "n": {"N": "0"}, "version": {"N": "1"}},
          {"pk": {"S": "p"}, "sk": {"S": "b"}, "n": {"N": "5"}, "version": {"N": "2"}},
      ],
  ))]
  calls = []

  async def fn(tx):
    calls.append(1)
    a, b = await tx.get(table, key("a")), await tx.get(table, key("b"))
    tx.put(table, {**a, "n": 1}, condition=Condition("#n < :ten", {"#n": "n"}, {":ten": 10}))
    tx.put(table, {**b, "n": 1})

  run(dynamo.run_transaction(fn))
  assert calls == [1, 1]


def test_an_oversized_item_is_terminal(table):
  dynamo = DynamoClient()
  calls = []

  async def too_big(tx):
    calls.append(1)
    tx.insert(table, {**key(), "blob": "x" * (401 * 1024)})

  with pytest.raises((ClientError, TransactionCanceledError)):
    run(dynamo.run_transaction(too_big))
  assert calls == [1]


def test_more_than_100_actions_are_refused_before_sending(table, record):
  dynamo = DynamoClient()
  sends = record(dynamo.client, "transact_write_items")

  async def too_many(tx):
    for i in range(101):
      tx.insert(table, {**key(str(i)), "n": i})

  with pytest.raises(TransactionUsageError):
    run(dynamo.run_transaction(too_many))
  assert sends.calls == []


def test_one_item_written_twice_is_one_action_with_every_condition(table, record):
  dynamo = DynamoClient()
  raw_put(table, {**key(), "n": 0, "version": 1})
  sends = record(dynamo.client, "transact_write_items")

  async def twice(tx):
    item = await tx.get(table, key())
    tx.put(table, {**item, "n": 1}, condition=Condition("#n = :zero", {"#n": "n"}, {":zero": 0}))
    tx.put(table, {**item, "n": 2})

  run(dynamo.run_transaction(twice))
  (action,) = sends.calls[0]["TransactItems"]
  assert action["Put"]["ConditionExpression"] == "(#dkver = :dkver) AND (#n = :zero)"
  assert raw_item(table, key()) == {**key(), "n": 2, "version": 2}


def test_unversioned_items_are_guarded_by_their_missing_version(table, record):
  dynamo = DynamoClient()
  raw_put(table, {**key(), "n": 0})
  sends = record(dynamo.client, "transact_write_items")
  run(dynamo.run_transaction(lambda tx: increment(tx, table)))
  put = sends.calls[0]["TransactItems"][0]["Put"]
  assert put["ConditionExpression"] == "attribute_exists(#dkkey) AND attribute_not_exists(#dkver)"
  assert raw_item(table, key()) == {**key(), "n": 1, "version": 1}


def test_query_reads_every_page_and_guards_the_items_it_writes(table, record):
  dynamo = DynamoClient()
  for i in range(23):
    raw_put(table, {**key(f"{i:02d}"), "n": i, "version": 1})
  sends = record(dynamo.client, "transact_write_items")

  async def bump_largest(tx):
    items = await tx.query(table, KeyConditionExpression=Key("pk").eq("p"), Limit=5)
    tx.put(table, {**items[-1], "n": 100})
    return len(items)

  assert run(dynamo.run_transaction(bump_largest)) == 23
  (action,) = sends.calls[0]["TransactItems"]
  assert action["Put"]["ExpressionAttributeValues"] == {":dkver": {"N": "1"}}
  assert raw_item(table, key("22")) == {**key("22"), "n": 100, "version": 2}


def test_query_refuses_reads_that_are_not_whole_consistent_items(table):
  dynamo = DynamoClient()

  async def by_index(tx):
    await tx.query(table, IndexName="by-owner", KeyConditionExpression=Key("owner").eq("me"))

  with pytest.raises(TransactionUsageError):
    run(dynamo.run_transaction(by_index))


def test_writing_an_item_that_was_not_read_is_refused(table):
  dynamo = DynamoClient()

  async def blind_put(tx):
    tx.put(table, {**key(), "n": 1})

  with pytest.raises(TransactionUsageError):
    run(dynamo.run_transaction(blind_put))


def test_reserved_placeholders_are_refused(table):
  dynamo = DynamoClient()

  async def clash(tx):
    tx.insert(table, {**key(), "n": 1}, condition=Condition("#dkver = :v", {"#dkver": "n"}, {":v": 1}))

  with pytest.raises(TransactionUsageError):
    run(dynamo.run_transaction(clash))


def test_writes_outside_the_running_transaction_are_refused(table):
  dynamo = DynamoClient()

  async def sneaky(tx):
    await dynamo.put(table.table_name, {**key(), "n": 1})

  with pytest.raises(TransactionUsageError):
    run(dynamo.run_transaction(sneaky))
  assert raw_item(table, key()) is None


def test_a_finished_attempt_takes_no_more_calls(table):
  dynamo = DynamoClient()
  kept = []

  async def keep(tx):
    kept.append(tx)

  run(dynamo.run_transaction(keep))
  with pytest.raises(TransactionUsageError):
    kept[0].insert(table, {**key(), "n": 1})


def test_a_nested_transaction_joins_and_commits_once(table, record):
  dynamo = DynamoClient()
  sends = record(dynamo.client, "transact_write_items")

  async def outer(tx):
    tx.insert(table, {**key("a"), "n": 1})
    return await dynamo.run_transaction(lambda inner: increment(inner, table, key("b")))

  run(dynamo.run_transaction(outer))
  assert len(sends.calls) == 1
  assert len(sends.calls[0]["TransactItems"]) == 2
  assert raw_item(table, key("b"))["n"] == 1


def test_a_nested_failure_discards_its_writes_and_keeps_its_reads_guarded(table, record):
  dynamo = DynamoClient()
  raw_put(table, {**key("b"), "n": 0, "version": 1})
  sends = record(dynamo.client, "transact_write_items")

  async def inner(tx):
    item = await tx.get(table, key("b"))
    tx.put(table, {**item, "n": 99})
    raise LookupError("inner refuses")

  async def outer(tx):
    tx.insert(table, {**key("a"), "n": 1})
    with pytest.raises(LookupError):
      await dynamo.run_transaction(inner)

  run(dynamo.run_transaction(outer))
  kinds = sorted(next(iter(a)) for a in sends.calls[0]["TransactItems"])
  assert kinds == ["ConditionCheck", "Put"]
  assert raw_item(table, key("b")) == {**key("b"), "n": 0, "version": 1}
  assert raw_item(table, key("a"))["n"] == 1


def test_a_nested_failure_keeps_the_guard_of_a_key_it_read_first(table, record):
  dynamo = DynamoClient()
  raw_put(table, {**key("b"), "n": 0, "version": 1})
  sends = record(dynamo.client, "transact_write_items")

  async def inner(tx):
    await tx.get(table, key("b"))
    raise LookupError("inner refuses")

  async def outer(tx):
    tx.check(table, key("b"), Condition("#n = :zero", {"#n": "n"}, {":zero": 0}))
    with pytest.raises(LookupError):
      await dynamo.run_transaction(inner)
    tx.insert(table, {**key("a"), "n": 1})

  run(dynamo.run_transaction(outer))
  check = next(a["ConditionCheck"] for a in sends.calls[0]["TransactItems"] if "ConditionCheck" in a)
  assert check["ConditionExpression"] == "(#dkver = :dkver) AND (#n = :zero)"


def test_a_nested_transaction_on_another_destination_is_refused(table):
  dynamo = DynamoClient()

  async def outer(tx):
    await DynamoClient(region_name="eu-west-1").run_transaction(lambda inner: increment(inner, table))

  with pytest.raises(TransactionUsageError):
    run(dynamo.run_transaction(outer))


def test_nested_transactions_may_not_run_concurrently(table):
  dynamo = DynamoClient()

  async def slow(tx):
    await asyncio.sleep(0.01)

  async def outer(tx):
    await asyncio.gather(dynamo.run_transaction(slow), dynamo.run_transaction(slow))

  with pytest.raises(TransactionUsageError):
    run(dynamo.run_transaction(outer))


class Account(DynamoModel):
  model_config = ConfigDict(extra="forbid")

  pk: str
  sk: str
  balance: Decimal = Decimal("0")
  frozen: bool = False


def test_model_helpers_keep_the_same_guarantees(table, record):
  Account._dynamo_config = table
  dynamo = DynamoClient()
  sends = record(dynamo.client, "transact_write_items")

  async def open_account(tx):
    assert await tx.get_model(Account, "p", "acct") is None
    tx.insert_model(Account(pk="p", sk="acct", balance=Decimal("10")))

  run(dynamo.run_transaction(open_account))
  stored = run(Account.get("p", "acct", consistent_read=True))
  assert stored is not None
  assert (stored.balance, stored.frozen) == (Decimal("10"), False)

  async def freeze(tx):
    account = await tx.get_model(Account, "p", "acct")
    account.frozen = True
    tx.put_model(account)
    return account

  account = run(dynamo.run_transaction(freeze))
  assert account.get_dirty_fields() == set()
  assert raw_item(table, key("acct"))["version"] == 2
  assert sends.calls[1]["TransactItems"][0]["Put"]["Item"]["frozen"] == {"BOOL": True}


@pytest.mark.skipif(not local_answers(), reason="DynamoDB Local is not running")
def test_concurrent_increments_on_real_transactions_lose_no_update(table, backend):
  if backend != "local":
    pytest.skip("moto does not make concurrent commits atomic")
  dynamo = DynamoClient()

  async def many():
    await asyncio.gather(*(
        dynamo.run_transaction(lambda tx: increment(tx, table), max_attempts=60) for _ in range(20)
    ))

  run(many())
  assert raw_item(table, key()) == {**key(), "n": 20, "version": 20}
