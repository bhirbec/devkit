"""Optimistic DynamoDB transactions: consistent reads, version guards, a bounded rerun on conflict.

`DynamoClient.run_transaction(fn)` calls `fn(tx)`, where `tx` is an `OptimisticTransaction`. Reads
go through `tx` as strongly consistent GetItems, and each records the version it saw, or that the
item was absent. Writes are staged and sent at the end in one TransactWriteItems. Each written item
is guarded by the version that was read (or by its absence), and each item read with `tx.get` but
not written gets a ConditionCheck with the same guard. If a guard no longer holds, another writer
got there first: nothing was applied, and `fn` runs again from the start on fresh reads.

A commit whose answer is lost (a timeout, a 5xx, a dropped connection) is a different case. It may
already have landed, and running `fn` again would apply its change twice. The same request is resent
with the same ClientRequestToken instead. Within DynamoDB's 10-minute idempotency window that
applies the transaction at most once. If it still has no answer, `TransactionOutcomeUnknownError`
is raised, and `fn` is never rerun.

docs/aws.md has the full contract and the guarantees this does not give.
"""

import asyncio
import contextvars
import copy
import json
import logging
import random
import re
import time
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping, Optional, TypeVar

from boto3.dynamodb.types import Binary, TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError, HTTPClientError
from botocore.exceptions import ConnectionError as BotoConnectionError

if TYPE_CHECKING:
  from .dynamo import DynamoClient, DynamoConfig, DynamoModel

log = logging.getLogger(__name__)

T = TypeVar("T")
M = TypeVar("M", bound="DynamoModel")

# TransactWriteItems' limit on actions per call, ConditionChecks included (raised from 25 in 2022).
MAX_TRANSACTION_ACTIONS = 100
# Answers after which a commit may or may not have landed (botocore's own retries may hide an
# earlier attempt that did): resending the same token is safe. TransactionInProgressException
# means the token's first request is still running.
_RESEND_CODES = frozenset({
    "InternalServerError",
    "ServiceUnavailable",
    "RequestTimeout",
    "RequestTimeoutException",
    "TransactionInProgressException",
    "ThrottlingException",
    "ProvisionedThroughputExceededException",
    "RequestLimitExceeded",
})
# Cancellation reasons that a fresh attempt can get past: nothing was applied.
_RERUN_REASONS = frozenset({
    "TransactionConflict", "ThrottlingError", "ProvisionedThroughputExceeded",
})

_serializer = TypeSerializer()
_deserializer = TypeDeserializer()


async def _sleep(seconds: float) -> None:
  await asyncio.sleep(seconds)


def _backoff(attempt: int, base: float, cap: float) -> float:
  return random.uniform(0, min(cap, base * 2 ** (attempt - 1)))


# --- values ---------------------------------------------------------------------------------------


def _normalized(value: Any) -> Any:
  """`value` as TypeSerializer takes it: TypeSerializer refuses floats, so they become Decimals
  through their shortest repr, the number they print as."""
  if isinstance(value, float):
    return Decimal(repr(value))
  if isinstance(value, Mapping):
    return {k: _normalized(v) for k, v in value.items()}
  if isinstance(value, (list, tuple)):
    return [_normalized(v) for v in value]
  if isinstance(value, (set, frozenset)):
    return {_normalized(v) for v in value}
  return value


def to_attribute_value(value: Any) -> dict[str, Any]:
  """A Python value as a DynamoDB AttributeValue (`{"S": ...}`, `{"BOOL": ...}`, ...)."""
  return _serializer.serialize(_normalized(value))


def serialize_item(item: Mapping[str, Any]) -> dict[str, Any]:
  return {k: to_attribute_value(v) for k, v in item.items()}


def deserialize_item(raw: Mapping[str, Any]) -> dict[str, Any]:
  return {k: _deserializer.deserialize(v) for k, v in raw.items()}


# --- conditions -----------------------------------------------------------------------------------

_NAME_PLACEHOLDER = re.compile(r"#[A-Za-z0-9_]+")
_VALUE_PLACEHOLDER = re.compile(r":[A-Za-z0-9_]+")


def merge_placeholders(first: Mapping[str, Any], second: Mapping[str, Any]) -> dict[str, Any]:
  """Both mappings in one. A placeholder in both must be bound to the same thing in each."""
  merged = dict(first)
  for placeholder, bound in second.items():
    if placeholder in merged and merged[placeholder] != bound:
      raise ValueError(f"Placeholder {placeholder} is bound to two different things")
    merged[placeholder] = bound
  return merged


class Condition:
  """A condition expression with its own placeholders: `names` maps `#name` to an attribute name,
  `values` maps `:value` to a plain Python value."""

  __slots__ = ("expression", "names", "values")

  def __init__(
      self,
      expression: str,
      names: Optional[Mapping[str, str]] = None,
      values: Optional[Mapping[str, Any]] = None,
  ):
    if not expression or not expression.strip():
      raise ValueError("A condition needs an expression")
    for placeholder in names or {}:
      if not _NAME_PLACEHOLDER.fullmatch(placeholder):
        raise ValueError(f"Attribute name placeholder {placeholder!r} must look like #name")
    for placeholder in values or {}:
      if not _VALUE_PLACEHOLDER.fullmatch(placeholder):
        raise ValueError(f"Attribute value placeholder {placeholder!r} must look like :value")
    self.expression = expression
    self.names = dict(names or {})
    self.values = dict(values or {})

  def __and__(self, other: "Condition") -> "Condition":
    return Condition(
        f"({self.expression}) AND ({other.expression})",
        merge_placeholders(self.names, other.names),
        merge_placeholders(self.values, other.values),
    )

  def __repr__(self) -> str:
    # Values are left out: they are often the data being written.
    return f"Condition({self.expression!r}, names={self.names!r})"

  def request_fields(self) -> dict[str, Any]:
    """The ConditionExpression and placeholder fields of a low-level request."""
    fields: dict[str, Any] = {"ConditionExpression": self.expression}
    if self.names:
      fields["ExpressionAttributeNames"] = dict(self.names)
    if self.values:
      fields["ExpressionAttributeValues"] = serialize_item(self.values)
    return fields


# --- errors ---------------------------------------------------------------------------------------


class DynamoTransactionError(Exception):
  """Base class for the errors `run_transaction` raises itself."""


class TransactionUsageError(DynamoTransactionError, ValueError):
  """The transaction was used in a way that cannot keep its guarantees, and nothing was sent."""


class CancellationReason:
  """Why DynamoDB canceled one action. `item` is the item as it stood when the action failed, or
  None when it was absent or DynamoDB did not return it."""

  __slots__ = ("table_name", "key", "code", "message", "item")

  def __init__(
      self, table_name: str, key: dict, code: str, message: Optional[str], item: Optional[dict]
  ):
    self.table_name = table_name
    self.key = key
    self.code = code
    self.message = message
    self.item = item

  def __repr__(self) -> str:
    return f"CancellationReason({self.table_name!r}, {self.key!r}, {self.code!r})"


class TransactionCanceledError(DynamoTransactionError):
  """DynamoDB canceled the commit for a reason another attempt would not fix, such as a
  ValidationError or ItemCollectionSizeLimitExceeded. Nothing was applied."""

  def __init__(self, message: str, reasons: list[CancellationReason]):
    super().__init__(message)
    self.reasons = reasons


class ConditionFailedError(TransactionCanceledError):
  """A condition passed with a write or a check failed, and every item the transaction read was
  still at the version it read: the rule does not hold now. Nothing was applied or rerun."""


class TransactionConflictError(DynamoTransactionError):
  """Every attempt lost a race: what it read changed before it committed. Nothing was applied by
  any of them."""

  def __init__(self, attempts: int, reasons: list[CancellationReason]):
    super().__init__(f"Transaction conflicted on each of its {attempts} attempts: {reasons}")
    self.attempts = attempts
    self.reasons = reasons


class TransactionOutcomeUnknownError(DynamoTransactionError):
  """The commit was sent and no answer said whether it landed, even after resending it with the same
  token. It was applied at most once. Resending `transact_items` with `client_request_token`, within
  ten minutes of the first send, settles it; running the transaction again with a new token could
  apply it twice."""

  def __init__(self, client_request_token: str, transact_items: list[dict], attempts: int):
    super().__init__(
        f"TransactWriteItems outcome unknown after {attempts} sends with ClientRequestToken "
        f"{client_request_token}"
    )
    self.client_request_token = client_request_token
    self.transact_items = transact_items
    self.attempts = attempts


def error_code(error: ClientError) -> str:
  return error.response.get("Error", {}).get("Code", "")


def _may_resend(error: ClientError) -> bool:
  status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") or 0
  return error_code(error) in _RESEND_CODES or status >= 500


async def send_transaction(
    dynamo: "DynamoClient",
    transact_items: list[dict],
    *,
    client_request_token: str,
    max_attempts: int,
    deadline_seconds: float,
) -> dict:
  """One TransactWriteItems, resent with the same token while its outcome is unknown. A definite
  answer (success, cancellation, validation, access) is returned or raised as it came."""
  started = time.monotonic()
  attempt = 0
  while True:
    attempt += 1
    try:
      return await dynamo._call(
          "transact_write_items",
          TransactItems=transact_items,
          ClientRequestToken=client_request_token,
      )
    except ClientError as e:
      if not _may_resend(e):
        raise
      failure: Exception = e
    except (BotoConnectionError, HTTPClientError) as e:
      # botocore has already retried internally, and one of those attempts may have landed.
      failure = e
    if attempt >= max_attempts or time.monotonic() - started >= deadline_seconds:
      unknown = TransactionOutcomeUnknownError(client_request_token, transact_items, attempt)
      raise unknown from failure
    log.warning(
        "TransactWriteItems outcome unknown (%s); resending with ClientRequestToken %s",
        type(failure).__name__,
        client_request_token,
    )
    await _sleep(_backoff(attempt, 0.1, 2.0))


# --- items in a transaction -----------------------------------------------------------------------


class _Observed:
  def __init__(self, name: str):
    self.name = name

  def __repr__(self) -> str:
    return self.name


ABSENT = _Observed("ABSENT")
UNVERSIONED = _Observed("UNVERSIONED")
UNREAD = _Observed("UNREAD")


def _spec(table: Any) -> "DynamoConfig":
  """The DynamoConfig of `table`: a DynamoConfig, or a DynamoModel class or instance."""
  spec = getattr(table, "_dynamo_config", table)
  if not all(hasattr(spec, a) for a in ("table_name", "pk_field", "sk_field", "version_field")):
    raise TypeError(f"Expected a DynamoConfig or a DynamoModel, got {table!r}")
  return spec


def _key_part(value: Any) -> list[str]:
  """A key attribute's value in one spelling per DynamoDB value: 1, 1.0 and Decimal("1.00") are
  one number, so they name one item."""
  if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
    number = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
    return ["N", format(number.normalize(), "f")]
  if isinstance(value, Binary):
    return ["B", value.value.hex()]
  if isinstance(value, (bytes, bytearray)):
    return ["B", value.hex()]
  return ["S", str(value)]


def _identity(spec: "DynamoConfig", key: Mapping[str, Any]) -> tuple[str, str]:
  canonical = {name: _key_part(value) for name, value in key.items()}
  return spec.table_name, json.dumps(canonical, sort_keys=True)


def _version_of(spec: "DynamoConfig", item: Mapping[str, Any]) -> Any:
  version = item.get(spec.version_field)
  if version is None:
    return UNVERSIONED
  if isinstance(version, bool) or not isinstance(version, (int, Decimal)) or Decimal(version) % 1:
    raise TransactionUsageError(
        f"{spec.table_name}: version attribute {spec.version_field!r} must be an integer, "
        f"got {version!r}"
    )
  return Decimal(version)


class _Entry:
  """One item in a transaction: what was read, what will be written, what must hold at commit."""

  __slots__ = ("spec", "key", "observed", "was_read", "read_item", "current", "guarded", "write",
               "conditions")

  def __init__(self, spec: "DynamoConfig", key: dict, observed: Any, was_read: bool,
               item: Optional[dict]):
    self.spec = spec
    self.key = key
    self.observed = observed
    self.was_read = was_read
    self.read_item = copy.deepcopy(item)
    self.current = copy.deepcopy(item)
    self.guarded = False
    self.write: Optional[str] = None
    self.conditions: list[Condition] = []

  def guard(self) -> Optional[Condition]:
    """The condition that holds while the item is as this transaction read it."""
    spec = self.spec
    if self.observed is UNREAD:
      return None
    if self.observed is ABSENT:
      return Condition("attribute_not_exists(#dkkey)", {"#dkkey": spec.pk_field})
    if self.observed is UNVERSIONED:
      return Condition(
          "attribute_exists(#dkkey) AND attribute_not_exists(#dkver)",
          {"#dkkey": spec.pk_field, "#dkver": spec.version_field},
      )
    return Condition("#dkver = :dkver", {"#dkver": spec.version_field}, {":dkver": self.observed})

  def guard_holds(self, item: Optional[dict]) -> bool:
    """Whether `item`, as DynamoDB returned it on a failed condition, is still as it was read."""
    if self.observed is UNREAD:
      return True  # No guard: only the caller's conditions could have failed.
    if self.observed is ABSENT:
      return item is None
    if item is None:
      return False
    if self.observed is UNVERSIONED:
      return self.spec.version_field not in item
    return item.get(self.spec.version_field) == self.observed

  def request(self) -> Optional[tuple[str, dict]]:
    """The TransactWriteItems action for this entry, if it needs one."""
    condition = self.guard()
    for extra in self.conditions:
      condition = extra if condition is None else condition & extra
    spec = self.spec
    if self.write == "put" and self.current is not None:
      version = 1 if self.observed in (ABSENT, UNVERSIONED) else int(self.observed) + 1
      item = {**self.current, spec.version_field: version}
      body: dict[str, Any] = {"TableName": spec.table_name, "Item": serialize_item(item)}
      kind = "Put"
    elif self.write == "delete" and self.observed not in (ABSENT, UNREAD):
      body = {"TableName": spec.table_name, "Key": serialize_item(self.key)}
      kind = "Delete"
    elif condition is not None and (self.guarded or self.conditions):
      body = {"TableName": spec.table_name, "Key": serialize_item(self.key)}
      kind = "ConditionCheck"
    else:
      return None
    assert condition is not None  # Put and Delete need a read first, so they always have a guard.
    body.update(condition.request_fields())
    # The item as it stood is what tells a lost race from a failed condition (_classify).
    body["ReturnValuesOnConditionCheckFailure"] = "ALL_OLD"
    return kind, body


_GUARD_PLACEHOLDERS = frozenset({"#dkkey", "#dkver", ":dkver"})


def _add_condition(entry: _Entry, condition: Condition) -> None:
  """Add `condition` to what must hold for `entry` at commit, refusing placeholders it cannot share
  with the guard or the entry's other conditions."""
  reserved = _GUARD_PLACEHOLDERS & (condition.names.keys() | condition.values.keys())
  if reserved:
    raise TransactionUsageError(f"Placeholders {sorted(reserved)} are reserved for version guards")
  try:
    for existing in entry.conditions:
      _ = condition & existing
  except ValueError as e:
    raise TransactionUsageError(str(e)) from None
  entry.conditions.append(condition)


_CURRENT: contextvars.ContextVar[Optional["OptimisticTransaction"]] = contextvars.ContextVar(
    "devkit_dynamo_transaction", default=None
)


def refuse_inside_transaction(operation: str) -> None:
  """Raise if a transaction function is running in this context: a write made outside it is not
  part of its commit, and it would be made again by every rerun."""
  current = _CURRENT.get()
  if current is not None and current._open:
    raise TransactionUsageError(
        f"{operation} is not part of the running transaction and would be repeated by its reruns; "
        "stage it on the transaction, or make it after run_transaction returns"
    )


class OptimisticTransaction:
  """One attempt of `DynamoClient.run_transaction`. Reads are strongly consistent and recorded,
  writes are staged, and the commit guards both. Belongs to the event loop that runs it."""

  def __init__(self, client: "DynamoClient"):
    self._client = client
    self._entries: dict[tuple[str, str], _Entry] = {}
    self._models: list[Any] = []
    self._open = True
    self._frames: list[Optional[asyncio.Task]] = []

  # --- reads

  async def get(self, table: Any, key: Mapping[str, Any]) -> Optional[dict]:
    """The item under `key`, as this transaction sees it, its own staged writes included. Read once
    with ConsistentRead, then guarded: the commit fails and reruns if it changed meanwhile."""
    self._check_open()
    spec = _spec(table)
    key = spec.checked_key(key)
    identity = _identity(spec, key)
    entry = self._entries.get(identity)
    if entry is None or (not entry.was_read and entry.observed is UNREAD):
      item = await self._client._consistent_get(spec.table_name, key)
      entry = self._observe(spec, key, item)
    entry.guarded = True
    return copy.deepcopy(entry.current)

  async def query(
      self, table: Any, *, max_items: Optional[int] = None, **kwargs: Any
  ) -> list[dict]:
    """Every page of a base-table query (Table.query keyword arguments), read with ConsistentRead.
    Returned items may be written, guarded by the version seen here. They are not guarded when only
    read: a rule decided from a set of items needs an item it reads with `get` that every change to
    the set also writes. Items this transaction inserted are not added to the results."""
    self._check_open()
    spec = _spec(table)
    refused = {"IndexName", "ProjectionExpression", "Select", "AttributesToGet", "ConsistentRead"}
    if refused & kwargs.keys():
      raise TransactionUsageError(
          f"tx.query reads whole base-table items consistently; it does not take "
          f"{sorted(refused & kwargs.keys())}. Query an index with DynamoClient.query, then "
          "tx.get each key."
      )
    items = await self._client.query_all(
        spec.table_name, max_items=max_items, ConsistentRead=True, **kwargs
    )
    found = []
    for item in items:
      key = spec.key_of(item)
      entry = self._entries.get(_identity(spec, key))
      if entry is None or (not entry.was_read and entry.observed is UNREAD):
        entry = self._observe(spec, key, item)
      if entry.current is not None:
        found.append(copy.deepcopy(entry.current))
    return found

  # --- writes

  def put(
      self, table: Any, item: Mapping[str, Any], *, condition: Optional[Condition] = None
  ) -> None:
    """Write the whole item, which this transaction read (or found absent) first. Its version
    attribute is set at commit to the read version plus one."""
    entry = self._entry_for_write(table, item, "put")
    if entry.observed is UNREAD:
      raise TransactionUsageError(
          f"{entry.spec.table_name} {entry.key} is written without being read; read it with get() "
          "or query(), or create it with insert()"
      )
    self._stage(entry, "put", item, condition)

  def insert(
      self, table: Any, item: Mapping[str, Any], *, condition: Optional[Condition] = None
  ) -> None:
    """Create an item that must not exist at commit. It need not be read first. If one appears
    meanwhile, the transaction reruns like any other conflict."""
    entry = self._entry_for_write(table, item, "insert")
    if entry.current is not None:
      raise TransactionUsageError(f"{entry.spec.table_name} {entry.key} is inserted over an item")
    if entry.observed is UNREAD:
      entry.observed = ABSENT
    self._stage(entry, "put", item, condition)

  def delete(
      self, table: Any, key: Mapping[str, Any], *, condition: Optional[Condition] = None
  ) -> None:
    """Delete the item under `key`, which this transaction read first. Deleting an absent item
    writes nothing."""
    self._check_open()
    spec = _spec(table)
    key = spec.checked_key(key)
    entry = self._entries.get(_identity(spec, key))
    if entry is None or entry.observed is UNREAD:
      raise TransactionUsageError(f"{spec.table_name} {key} is deleted without being read first")
    self._stage(entry, "delete", None, condition)

  def check(self, table: Any, key: Mapping[str, Any], condition: Condition) -> None:
    """Require `condition` of the item under `key` at commit, without writing it. Combined with its
    version guard if this transaction read it. A failure is a ConditionFailedError, not a rerun."""
    self._check_open()
    spec = _spec(table)
    key = spec.checked_key(key)
    identity = _identity(spec, key)
    entry = self._entries.get(identity)
    if entry is None:
      entry = self._entries[identity] = _Entry(spec, key, UNREAD, False, None)
    _add_condition(entry, condition)

  # --- models

  async def get_model(self, model_cls: type[M], pk_value: Any, sk_value: Any = None) -> Optional[M]:
    """`get` for a DynamoModel, validated by its class."""
    spec = _spec(model_cls)
    item = await self.get(spec, spec.key(pk_value, sk_value))
    return None if item is None else model_cls.from_item(item)

  def put_model(self, model: "DynamoModel", *, condition: Optional[Condition] = None) -> None:
    """`put` of a model's stored fields (relations excluded); its dirty fields clear on commit."""
    self.put(type(model), model._model_dump_without_relations(), condition=condition)
    self._models.append(model)

  def insert_model(self, model: "DynamoModel", *, condition: Optional[Condition] = None) -> None:
    self.insert(type(model), model._model_dump_without_relations(), condition=condition)
    self._models.append(model)

  def delete_model(self, model: "DynamoModel", *, condition: Optional[Condition] = None) -> None:
    self.delete(type(model), model.primary_key, condition=condition)
    self._models.append(model)

  # --- internals

  def _check_open(self) -> None:
    if not self._open:
      raise TransactionUsageError("This transaction attempt has finished; it takes no more calls")

  def _observe(self, spec: "DynamoConfig", key: dict, item: Optional[dict]) -> _Entry:
    identity = _identity(spec, key)
    existing = self._entries.get(identity)
    if existing is not None and (existing.was_read or existing.observed is not UNREAD):
      return existing  # Another read of the same key finished first.
    entry = _Entry(spec, key, ABSENT if item is None else _version_of(spec, item), True, item)
    if existing is not None:
      entry.conditions = existing.conditions
    self._entries[identity] = entry
    return entry

  def _entry_for_write(self, table: Any, item: Mapping[str, Any], operation: str) -> _Entry:
    self._check_open()
    spec = _spec(table)
    key = spec.key_of(item)
    identity = _identity(spec, key)
    entry = self._entries.get(identity)
    if entry is None:
      if operation != "insert":
        raise TransactionUsageError(
            f"{spec.table_name} {key} is written without being read; read it with get() or "
            "query(), or create it with insert()"
        )
      entry = self._entries[identity] = _Entry(spec, key, UNREAD, False, None)
    return entry

  def _stage(self, entry: _Entry, write: str, item: Optional[Mapping[str, Any]],
             condition: Optional[Condition]) -> None:
    if condition is not None:
      _add_condition(entry, condition)
    if write == "put":
      assert item is not None
      staged = copy.deepcopy(dict(item))
      if entry.observed in (ABSENT, UNVERSIONED, UNREAD):
        staged.pop(entry.spec.version_field, None)
      else:
        staged[entry.spec.version_field] = entry.observed
      entry.current = staged
    else:
      entry.current = None
    entry.write = write

  def _requests(self) -> list[tuple[_Entry, dict]]:
    requests = []
    for entry in self._entries.values():
      action = entry.request()
      if action is not None:
        requests.append((entry, {action[0]: action[1]}))
    if len(requests) > MAX_TRANSACTION_ACTIONS:
      raise TransactionUsageError(
          f"A transaction of {len(requests)} actions is over DynamoDB's {MAX_TRANSACTION_ACTIONS}"
      )
    return requests

  def _snapshot(self) -> dict:
    return {
        identity: (e.observed, copy.deepcopy(e.current), e.write, list(e.conditions))
        for identity, e in self._entries.items()
    }

  def _restore(self, snapshot: dict, models: int) -> None:
    """Undo the writes staged since `snapshot`, keeping the reads and their guards."""
    for identity in list(self._entries):
      entry = self._entries[identity]
      before = snapshot.get(identity)
      if before is not None and not (entry.was_read and before[0] is UNREAD):
        entry.observed, entry.current, entry.write, entry.conditions = before
      elif entry.was_read:
        # First read since the snapshot: keep the read, drop what was staged on it.
        entry.current, entry.write = copy.deepcopy(entry.read_item), None
        entry.conditions = list(before[3]) if before is not None else []
      else:
        del self._entries[identity]
    del self._models[models:]

  async def _join(self, fn: Callable[["OptimisticTransaction"], Awaitable[T]]) -> T:
    task = asyncio.current_task()
    if self._frames and self._frames[-1] is not task:
      raise TransactionUsageError("Nested transactions joining one may not run concurrently")
    snapshot, models = self._snapshot(), len(self._models)
    self._frames.append(task)
    try:
      return await fn(self)
    except BaseException:
      self._restore(snapshot, models)
      raise
    finally:
      self._frames.pop()


def _classify(
    requests: list[tuple[_Entry, dict]], error: ClientError
) -> tuple[str, list[CancellationReason]]:
  """What a cancellation calls for: "rerun" when a guard no longer held or DynamoDB asked for a
  retry, "failed" when only conditions the caller gave failed, "terminal" otherwise."""
  raw = error.response.get("CancellationReasons") or []
  reasons: list[CancellationReason] = []
  rerun = terminal = False
  for (entry, _), reason in zip(requests, raw):
    code = reason.get("Code")
    if not code or code == "None":
      continue
    item = deserialize_item(reason["Item"]) if reason.get("Item") else None
    reasons.append(
        CancellationReason(entry.spec.table_name, entry.key, code, reason.get("Message"), item)
    )
    if code == "ConditionalCheckFailed":
      # The guard held on the item as it stood, so one of the caller's conditions failed.
      rerun = rerun or not (entry.conditions and entry.guard_holds(item))
    elif code in _RERUN_REASONS:
      rerun = True
    else:
      terminal = True
  if terminal or not reasons or len(raw) != len(requests):
    return "terminal", reasons
  # A lost race comes first: the failed condition may have been decided from what changed.
  return ("rerun" if rerun else "failed"), reasons


async def run(
    client: "DynamoClient",
    fn: Callable[[OptimisticTransaction], Awaitable[T]],
    *,
    max_attempts: int,
    max_commit_attempts: int,
    commit_deadline_seconds: float,
) -> T:
  if max_attempts < 1 or max_commit_attempts < 1:
    raise ValueError("max_attempts and max_commit_attempts must be at least 1")
  current = _CURRENT.get()
  if current is not None and current._open:
    if current._client.target != client.target:
      raise TransactionUsageError(
          "A nested run_transaction on another DynamoDB destination cannot join the running one"
      )
    return await current._join(fn)

  reasons: list[CancellationReason] = []
  for attempt in range(1, max_attempts + 1):
    tx = OptimisticTransaction(client)
    context_token = _CURRENT.set(tx)
    try:
      result = await fn(tx)
    finally:
      tx._open = False
      _CURRENT.reset(context_token)
    requests = tx._requests()
    if not any(next(iter(r)) != "ConditionCheck" for _, r in requests):
      return result  # Nothing to write: reads alone commit nothing.
    try:
      await send_transaction(
          client,
          [r for _, r in requests],
          client_request_token=str(uuid.uuid4()),
          max_attempts=max_commit_attempts,
          deadline_seconds=commit_deadline_seconds,
      )
    except ClientError as e:
      if error_code(e) != "TransactionCanceledException":
        raise
      verdict, reasons = _classify(requests, e)
      if verdict == "failed":
        raise ConditionFailedError(f"Transaction condition failed: {reasons}", reasons) from e
      if verdict == "terminal":
        message = f"Transaction canceled: {reasons or error_code(e)}"
        raise TransactionCanceledError(message, reasons) from e
      log.debug("Transaction attempt %d lost a race: %s", attempt, reasons)
      if attempt < max_attempts:
        await _sleep(_backoff(attempt, 0.02, 1.0))
      continue
    for model in tx._models:
      model.clear_dirty_fields()
    return result
  raise TransactionConflictError(max_attempts, reasons)
