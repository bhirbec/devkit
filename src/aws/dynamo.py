"""DynamoDB helpers: `DynamoModel` (Pydantic models stored in a table), `DynamoClient` (async calls
for one destination, run on a thread pool) and two kinds of transaction.

`DynamoClient.transaction()` stages fixed writes, each under its own condition, and sends them in
one TransactWriteItems. `DynamoClient.run_transaction(fn)` reads consistently, guards what it read
and reruns on conflict (transaction.py). docs/aws.md compares them and lists what neither
guarantees.
"""

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import (Any, Awaitable, Callable, ClassVar, Dict, Generic, List, Mapping, Optional, Set,
                    Type, TypeVar, TypedDict, cast)

from boto3.dynamodb.conditions import Key
from botocore.config import Config
from pydantic import BaseModel, Field, ValidationError

from . import clients, transaction
from .transaction import (MAX_TRANSACTION_ACTIONS, OptimisticTransaction, deserialize_item,
                          merge_placeholders, refuse_inside_transaction, send_transaction,
                          serialize_item, to_attribute_value)

# Define TypeVars for models that inherit from BaseModel
T = TypeVar("T", bound=BaseModel)
# Self type for the DynamoModel class
Self = TypeVar("Self", bound="DynamoModel")
R = TypeVar("R")


class DynamoDBOperationDict(TypedDict, total=False):
  TableName: str
  Key: Dict[str, Any]
  Item: Dict[str, Any]
  UpdateExpression: str
  ExpressionAttributeNames: Dict[str, str]
  ExpressionAttributeValues: Dict[str, Any]
  ConditionExpression: str


# TODO: we should be able to control the number of threads
# Create a thread pool with a reasonable number of workers
executor = ThreadPoolExecutor(max_workers=10)

# The resource behind the Table-style calls keeps the settings this module always used.
_RESOURCE_CONFIG = Config(
    max_pool_connections=50,
    retries={"max_attempts": 3, "mode": "standard"},
)
# The low-level client sends transactions and their reads. Short timeouts bound how long a commit
# can be resent, keeping every resend well inside DynamoDB's 10-minute idempotency window.
_CLIENT_CONFIG = Config(
    max_pool_connections=50,
    retries={"max_attempts": 3, "mode": "standard"},
    connect_timeout=5,
    read_timeout=10,
)


class DynamoConfig:
  """A table, its key fields, the relation fields its model keeps elsewhere, and the attribute
  `run_transaction` keeps each item's version in."""

  def __init__(
      self,
      table_name: str,
      pk_field: str,
      sk_field: Optional[str] = None,
      relations: Optional[List[str]] = None,
      version_field: str = "version",
  ):
    if version_field in (pk_field, sk_field):
      raise ValueError(f"{table_name}: the version field cannot be a key field")
    self.table_name = table_name
    self.pk_field = pk_field
    self.sk_field = sk_field
    self.relations = relations or []
    self.version_field = version_field

  @property
  def key_fields(self) -> tuple:
    return (self.pk_field, self.sk_field) if self.sk_field else (self.pk_field,)

  def key(self, pk_value: Any, sk_value: Any = None) -> Dict[str, Any]:
    """The primary key for these values; a table with a sort key needs both."""
    if self.sk_field is None:
      if sk_value is not None:
        raise ValueError(f"{self.table_name} has no sort key")
      return self.checked_key({self.pk_field: pk_value})
    return self.checked_key({self.pk_field: pk_value, self.sk_field: sk_value})

  def key_of(self, item: Mapping[str, Any]) -> Dict[str, Any]:
    """The primary key of `item`."""
    return self.checked_key({field: item.get(field) for field in self.key_fields})

  def checked_key(self, key: Mapping[str, Any]) -> Dict[str, Any]:
    """`key`, refused unless it holds exactly this table's key attributes, none of them None."""
    if set(key) != set(self.key_fields) or any(v is None for v in key.values()):
      raise ValueError(
          f"{self.table_name} key needs exactly {list(self.key_fields)}, got {dict(key)!r}"
      )
    return dict(key)


class DynamoModel(BaseModel, Generic[T]):
  """Mixin that adds DynamoDB operations to Pydantic models."""

  # Class variables to be defined in child classes
  _dynamo_config: ClassVar[DynamoConfig]

  # Common fields for all models
  created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

  def __init__(self, **data):
    super().__init__(**data)
    # Initialize instance-specific empty dirty_fields set with double underscore for name mangling
    self.__dirty_fields = set()

  def __setattr__(self, name, value):
    # Check if this is an actual field in the model (not a private attribute)
    if name in self.__class__.model_fields and hasattr(self, name):
      # Prevent modification of key fields
      pk_field = self.__class__._dynamo_config.pk_field
      sk_field = self.__class__._dynamo_config.sk_field

      if name == pk_field:
        raise ValueError(f"Cannot modify primary key field '{pk_field}'. Use put() instead.")

      if sk_field and name == sk_field:
        raise ValueError(f"Cannot modify sort key field '{sk_field}'. Use put() instead.")

      # If the value is different from the current value, mark as dirty
      if getattr(self, name) != value:
        self.__dirty_fields.add(name)

    # Call parent's __setattr__ to actually set the attribute
    super().__setattr__(name, value)

  def clear_dirty_fields(self) -> None:
    """Clear all dirty fields. Useful when you want to reset the dirty state."""
    self.__dirty_fields.clear()

  def get_dirty_fields(self) -> Set[str]:
    """Get the set of dirty fields. Returns a copy to prevent external modification."""
    return self.__dirty_fields.copy()

  def _model_dump_without_relations(self, **kwargs):
    """Get model data for database operations, excluding relation fields."""
    data = self.model_dump(**kwargs)
    # Remove relation fields when serializing for database
    for relation_field in self.__class__._dynamo_config.relations:
      data.pop(relation_field, None)
    return data

  @classmethod
  def from_item(cls: Type[Self], item: Mapping[str, Any]) -> Self:
    """The model of a stored item. The version attribute `run_transaction` writes is dropped unless
    the model declares it, so models that forbid extra fields still load such items."""
    version_field = cls._dynamo_config.version_field
    if version_field in item and version_field not in cls.model_fields:
      item = {k: v for k, v in item.items() if k != version_field}
    return cls(**item)

  @property
  def primary_key(self) -> Dict[str, Any]:
    """Create a key dictionary for DynamoDB operations."""
    # Get primary key value
    pk_field = self.__class__._dynamo_config.pk_field
    pk_value = getattr(self, pk_field)

    # Create the base key dictionary
    key = {pk_field: pk_value}

    # Add sort key if defined
    sk_field = self.__class__._dynamo_config.sk_field
    if sk_field:
      sk_value = getattr(self, sk_field)
      if sk_value is not None:
        key[sk_field] = sk_value

    return key

  @classmethod
  async def get(
      cls: Type[Self],
      pk_value: str,
      sk_value: Optional[str] = None,
      *,
      consistent_read: bool = False,
  ) -> Optional[Self]:
    """
    Retrieve an item by primary key and optional sort key.

    Args:
        pk_value: The value of the partition key
        sk_value: The value of the sort key (if the table has a sort key)
        consistent_read: Read with ConsistentRead, seeing every write acknowledged before the read

    Returns:
        An instance of the model or None if not found
    """
    # Create the key dictionary
    key = {cls._dynamo_config.pk_field: pk_value}

    # Add sort key if provided and defined in config
    if sk_value is not None and cls._dynamo_config.sk_field:
      key[cls._dynamo_config.sk_field] = sk_value

    async with DynamoClient() as dynamodb:
      item = await dynamodb.get(
          cls._dynamo_config.table_name,
          key,
          consistent_read=consistent_read,
      )
      if not item:
        return None
      try:
        return cls.from_item(item)
      except ValidationError as exc:
        pk_field = cls._dynamo_config.pk_field
        sk_field = cls._dynamo_config.sk_field
        sk_value_str = f", {sk_field}={sk_value}" if sk_field and sk_value else ""
        raise ValueError(
            f"Invalid record for {cls.__name__} {pk_field}={pk_value}{sk_value_str}"
        ) from exc

  @classmethod
  async def query(cls: Type[Self], **kwargs) -> List[Self]:
    """One page of a Table.query (at most Limit items or 1 MB); query_all reads every page."""
    async with DynamoClient() as dynamodb:
      response = await dynamodb.query(cls._dynamo_config.table_name, **kwargs)
      items = response.get("Items", [])
      return [cls.from_item(item) for item in items]

  @classmethod
  async def query_all(cls: Type[Self], *, max_items: Optional[int] = None, **kwargs) -> List[Self]:
    """Every page of a Table.query, up to `max_items` models; Limit sets the page size."""
    async with DynamoClient() as dynamodb:
      items = await dynamodb.query_all(cls._dynamo_config.table_name, max_items=max_items, **kwargs)
      return [cls.from_item(item) for item in items]

  async def put(
      self,
      condition_expression: Optional[str] = None,
      *,
      expression_attribute_names: Optional[Dict[str, str]] = None,
      expression_attribute_values: Optional[Dict[str, Any]] = None,
  ) -> None:
    """
    Put the model instance into DynamoDB, under an optional condition whose placeholders are
    passed in expression_attribute_names and expression_attribute_values.
    """
    async with DynamoClient() as dynamodb:
      await dynamodb.put(
          self.__class__._dynamo_config.table_name,
          self._model_dump_without_relations(),
          condition_expression,
          expression_attribute_names=expression_attribute_names,
          expression_attribute_values=expression_attribute_values,
      )

    # Clear dirty fields after successful delete
    self.clear_dirty_fields()

  async def update(
      self,
      condition_expression: Optional[str] = None,
      *,
      expression_attribute_names: Optional[Dict[str, str]] = None,
      expression_attribute_values: Optional[Dict[str, Any]] = None,
  ) -> None:
    """
    Update only the modified fields in DynamoDB.
    """
    dirty_fields = self.get_dirty_fields()
    if not dirty_fields:
      raise ValueError("Model does not have any fields to update")

    # Serialize the model to properly handle nested objects, then extract only dirty fields
    serialized_data = self._model_dump_without_relations()
    serialized_updates = {f: serialized_data[f] for f in dirty_fields}

    async with DynamoClient() as dynamodb:
      await dynamodb.update(
          self.__class__._dynamo_config.table_name,
          key=self.primary_key,
          updates=serialized_updates,
          condition_expression=condition_expression,
          expression_attribute_names=expression_attribute_names,
          expression_attribute_values=expression_attribute_values,
      )

    # Clear dirty fields after successful update
    self.clear_dirty_fields()

  async def delete(
      self,
      condition_expression: Optional[str] = None,
      *,
      expression_attribute_names: Optional[Dict[str, str]] = None,
      expression_attribute_values: Optional[Dict[str, Any]] = None,
  ) -> None:
    """
    Delete the model instance from DynamoDB.
    """
    async with DynamoClient() as dynamodb:
      await dynamodb.delete(
          self.__class__._dynamo_config.table_name,
          self.primary_key,
          condition_expression,
          expression_attribute_names=expression_attribute_names,
          expression_attribute_values=expression_attribute_values,
      )

    # Clear dirty fields after successful delete
    self.clear_dirty_fields()

  @classmethod
  async def batch_delete_partition(
      cls,
      pk_value: Any,
      *,
      sk_prefix: Optional[str] = None,   # optional: limit to a subset
      page_limit: int = 1000
  ) -> Dict[str, Any]:
    """
    Delete all items under a given partition key (optionally only those with a given SK prefix).
    Streams pages and deletes each page in batches (25) with retries via batch_writer.
    """
    refuse_inside_transaction("batch_delete_partition")
    async with DynamoClient() as dynamodb:
      table_name = cls._dynamo_config.table_name

      # Get PK/SK field names from model config
      pk_field = cls._dynamo_config.pk_field
      sk_field = cls._dynamo_config.sk_field

      # Build KeyConditionExpression safely
      kce = Key(pk_field).eq(pk_value)
      if sk_field and sk_prefix:
        kce = kce & Key(sk_field).begins_with(sk_prefix)

      # Use aliases to avoid reserved-word issues in ProjectionExpression
      expr_attr_names = {"#pk": pk_field}
      projection = "#pk"
      if sk_field:
        expr_attr_names["#sk"] = sk_field
        projection += ", #sk"

      deleted = 0
      start_key = None

      while True:
        query_params = {
            "KeyConditionExpression": kce,
            "ProjectionExpression": projection,
            "ExpressionAttributeNames": expr_attr_names,
            "Limit": page_limit,
        }

        # Only include ExclusiveStartKey if it's not None
        if start_key is not None:
          query_params["ExclusiveStartKey"] = start_key

        resp = await dynamodb.query(table_name, **query_params)
        items = resp.get("Items", [])
        if not items:
          break

        # Build the page's delete keys
        page_keys = []
        for it in items:
          dk = {pk_field: it[pk_field]}
          if sk_field and sk_field in it:
            dk[sk_field] = it[sk_field]
          page_keys.append(dk)

        # Synchronous batch_writer with internal 25-item batching + retries. The Table is fetched on
        # the worker thread: boto3 resources are not thread-safe.
        def _delete_page():
          with dynamodb.get_table(table_name).batch_writer() as batch:
            for dk in page_keys:
              batch.delete_item(Key=dk)

        await dynamodb._run_in_executor(_delete_page)
        deleted += len(page_keys)

        start_key = resp.get("LastEvaluatedKey")
        if not start_key:
          break

      return {"deleted_count": deleted, "unprocessed_keys": []}


def _set_expression(updates: Mapping[str, Any]) -> tuple[str, Dict[str, str], Dict[str, Any]]:
  """A SET expression for `updates`. Its placeholders are #updN and :updN, so a caller's condition
  may use any others."""
  if not updates:
    raise ValueError("No fields to update")
  names: Dict[str, str] = {}
  values: Dict[str, Any] = {}
  parts = []
  for i, (field_name, value) in enumerate(updates.items()):
    names[f"#upd{i}"] = field_name
    values[f":upd{i}"] = value
    parts.append(f"#upd{i} = :upd{i}")
  return "SET " + ", ".join(parts), names, values


def _add_condition(
    params: Dict[str, Any],
    condition_expression: Optional[str],
    names: Optional[Mapping[str, str]],
    values: Optional[Mapping[str, Any]],
) -> None:
  """Add a caller's condition and its placeholders to request `params`, which may already hold
  generated placeholders; one bound differently on each side is refused."""
  if not condition_expression:
    if names or values:
      raise ValueError("Expression attribute names or values need a condition expression")
    return
  params["ConditionExpression"] = condition_expression
  if names:
    params["ExpressionAttributeNames"] = merge_placeholders(
        params.get("ExpressionAttributeNames", {}), names
    )
  if values:
    params["ExpressionAttributeValues"] = merge_placeholders(
        params.get("ExpressionAttributeValues", {}), values
    )


class DynamoTransaction:
  """Writes staged in `DynamoClient.transaction()` and sent in one TransactWriteItems when the
  block ends, each under its own condition. Nothing is read or rerun on a conflict: that is
  `DynamoClient.run_transaction`. Condition values are plain Python values."""

  def __init__(self, client: "DynamoClient"):
    self._statements = []
    self._models = []  # Track models used in this transaction
    self._client = client

  def put(
      self,
      model: DynamoModel,
      condition_expression: Optional[str] = None,
      *,
      expression_attribute_names: Optional[Dict[str, str]] = None,
      expression_attribute_values: Optional[Dict[str, Any]] = None,
  ) -> None:
    """
    Create a Put statement for a transaction and add it to the transaction.
    This can be used both for individual put operations and as part of transactions.
    """
    # Convert model to dict, excluding relation fields
    items = model._model_dump_without_relations()

    # Create the base put dictionary
    put_dict: DynamoDBOperationDict = {
        "TableName": model.__class__._dynamo_config.table_name,
        "Item": {k: self.to_dynamodb_value(v) for k, v in items.items()},
    }
    self._add_condition(
        put_dict, condition_expression, expression_attribute_names, expression_attribute_values
    )

    # Create the complete statement and track the model
    self._statements.append({"Put": put_dict})
    self._models.append(model)

  def update(
      self,
      model: DynamoModel,
      condition_expression: Optional[str] = None,
      *,
      expression_attribute_names: Optional[Dict[str, str]] = None,
      expression_attribute_values: Optional[Dict[str, Any]] = None,
  ) -> None:
    """
    Create an Update statement for a transaction using dirty fields and add it to the transaction.
    """
    dirty_fields = model.get_dirty_fields()
    if not dirty_fields:
      raise ValueError("Model does not have any fields to update")

    # Serialize the model to properly handle nested objects, then extract only dirty fields
    serialized_data = model._model_dump_without_relations()
    serialized_updates = {f: serialized_data[f] for f in dirty_fields}
    update_expression, expr_attr_names, expr_attr_values = _set_expression(serialized_updates)

    # Create the base update dictionary
    update_dict: DynamoDBOperationDict = {
        "TableName": model.__class__._dynamo_config.table_name,
        "Key": self._formatted_primary_key(model),
        "UpdateExpression": update_expression,
        "ExpressionAttributeNames": expr_attr_names,
        "ExpressionAttributeValues": {
            k: self.to_dynamodb_value(v)
            for k, v in expr_attr_values.items()
        },
    }
    self._add_condition(
        update_dict, condition_expression, expression_attribute_names, expression_attribute_values
    )

    # Create the complete statement and track the model
    self._statements.append({"Update": update_dict})
    self._models.append(model)

  def delete(
      self,
      model: DynamoModel,
      condition_expression: Optional[str] = None,
      *,
      expression_attribute_names: Optional[Dict[str, str]] = None,
      expression_attribute_values: Optional[Dict[str, Any]] = None,
  ) -> None:
    """
    Create a Delete statement for a transaction and add it to the transaction.
    """
    # Create the base delete dictionary
    delete_dict: DynamoDBOperationDict = {
        "TableName": model.__class__._dynamo_config.table_name,
        "Key": self._formatted_primary_key(model),
    }
    self._add_condition(
        delete_dict, condition_expression, expression_attribute_names, expression_attribute_values
    )

    # Create the complete statement and track the model
    self._statements.append({"Delete": delete_dict})
    self._models.append(model)

  def condition_check(
      self,
      model: DynamoModel,
      condition_expression: str,
      *,
      expression_attribute_names: Optional[Dict[str, str]] = None,
      expression_attribute_values: Optional[Dict[str, Any]] = None,
  ) -> None:
    """Require a condition of the model's stored item for the transaction to commit, without
    writing it."""
    check_dict: DynamoDBOperationDict = {
        "TableName": model.__class__._dynamo_config.table_name,
        "Key": self._formatted_primary_key(model),
    }
    self._add_condition(
        check_dict, condition_expression, expression_attribute_names, expression_attribute_values
    )
    self._statements.append({"ConditionCheck": check_dict})

  async def execute(self) -> None:
    """Send the staged statements in one TransactWriteItems. A cancellation is raised as botocore's
    TransactionCanceledException; a lost response is resent with the same token."""
    if not self._statements:
      return  # No statements to execute
    refuse_inside_transaction("DynamoClient.transaction()")
    if len(self._statements) > MAX_TRANSACTION_ACTIONS:
      raise ValueError(
          f"A transaction of {len(self._statements)} statements is over DynamoDB's "
          f"{MAX_TRANSACTION_ACTIONS}"
      )

    try:
      await self._client.transact_write_items(list(self._statements))
    except Exception as e:
      raise e
    else:
      self._clear_dirty_fields()
    finally:
      self._reset()

  def to_dynamodb_value(self, value: Any) -> Dict[str, Any]:
    """Convert a Python value to a DynamoDB-typed value."""
    return to_attribute_value(value)

  def _add_condition(self, params, condition_expression, names, values) -> None:
    typed_values = {k: self.to_dynamodb_value(v) for k, v in (values or {}).items()}
    _add_condition(cast(Dict[str, Any], params), condition_expression, names, typed_values)

  def _formatted_primary_key(self, model: DynamoModel) -> Dict[str, Any]:
    """Create a DynamoDB-formatted key dictionary for transactions."""
    return {k: self.to_dynamodb_value(v) for k, v in model.primary_key.items()}

  def _reset(self) -> None:
    """Reset transaction state."""
    self._statements.clear()
    self._models.clear()

  def _clear_dirty_fields(self) -> None:
    """Clear dirty fields on all models that were part of this transaction."""
    for model in self._models:
      model.clear_dirty_fields()


class DynamoClient:
  """Async DynamoDB calls for one destination: a profile, a region and an endpoint, each optional.
  Every client for the same destination shares one boto3 client; each call runs on a thread pool."""

  def __init__(
      self,
      profile_name: str = "",
      *,
      region_name: Optional[str] = None,
      endpoint_url: Optional[str] = None,
  ):
    self.target = clients.AwsTarget(profile_name or None, region_name, endpoint_url)

  def __repr__(self) -> str:
    return f"DynamoClient({self.target!r})"

  @property
  def client(self) -> Any:
    """The shared low-level boto3 client for this destination."""
    return clients.shared_client("dynamodb", self.target, _CLIENT_CONFIG)

  async def __aenter__(self):
    # Reuse existing connection
    return self

  async def __aexit__(self, exc_type, exc, tb):
    # No need to close connection, it's reused
    pass

  def get_table(self, table_name: str):
    """The calling thread's boto3 Table. Resources are not thread-safe: use it on that thread."""
    return clients.thread_resource("dynamodb", self.target, _RESOURCE_CONFIG).Table(table_name)

  async def get(self, table_name: str, key: dict, *, consistent_read: bool = False):
    params: Dict[str, Any] = {"Key": key}
    if consistent_read:
      params["ConsistentRead"] = True
    # Run blocking operation in thread pool
    response = await self._run_in_executor(
        lambda: self.get_table(table_name).get_item(**params)
    )
    return response.get("Item")

  async def query(self, table_name: str, **kwargs):
    """One page of a Table.query; its LastEvaluatedKey, if any, starts the next."""
    # Run blocking operation in thread pool
    response = await self._run_in_executor(
        lambda: self.get_table(table_name).query(**kwargs)
    )
    return response

  async def query_all(
      self, table_name: str, *, max_items: Optional[int] = None, **kwargs
  ) -> List[dict]:
    """Every page of a Table.query, up to `max_items` items. Limit sets the page size."""
    items: List[dict] = []
    params = dict(kwargs)
    while True:
      page = dict(params)
      response = await self._run_in_executor(lambda: self.get_table(table_name).query(**page))
      items.extend(response.get("Items", []))
      if max_items is not None and len(items) >= max_items:
        return items[:max_items]
      start_key = response.get("LastEvaluatedKey")
      if not start_key:
        return items
      params["ExclusiveStartKey"] = start_key

  async def scan(self, table_name: str, **kwargs):
    """One page of a Table.scan; its LastEvaluatedKey, if any, starts the next."""
    # Run blocking operation in thread pool
    response = await self._run_in_executor(
        lambda: self.get_table(table_name).scan(**kwargs)
    )
    return response

  async def put(
      self,
      table_name: str,
      item: dict,
      condition_expression: Optional[str] = None,
      *,
      expression_attribute_names: Optional[Dict[str, str]] = None,
      expression_attribute_values: Optional[Dict[str, Any]] = None,
  ):
    refuse_inside_transaction("DynamoClient.put")
    # Prepare put parameters
    put_params: Dict[str, Any] = {"Item": item}
    _add_condition(
        put_params, condition_expression, expression_attribute_names, expression_attribute_values
    )

    # Run blocking operation in thread pool
    await self._run_in_executor(
        lambda: self.get_table(table_name).put_item(**put_params)
    )

  async def update(
      self,
      table_name: str,
      key: dict,
      updates: Dict[str, Any],
      condition_expression: Optional[str] = None,
      *,
      expression_attribute_names: Optional[Dict[str, str]] = None,
      expression_attribute_values: Optional[Dict[str, Any]] = None,
  ):
    refuse_inside_transaction("DynamoClient.update")
    update_expression, expr_attr_names, expr_attr_values = _set_expression(updates)

    # Prepare update parameters
    update_params: Dict[str, Any] = {
        "Key": key,
        "UpdateExpression": update_expression,
        "ExpressionAttributeNames": expr_attr_names,
        "ExpressionAttributeValues": expr_attr_values,
    }
    _add_condition(
        update_params, condition_expression, expression_attribute_names, expression_attribute_values
    )

    await self._run_in_executor(
        lambda: self.get_table(table_name).update_item(**update_params)
    )

  async def delete(
      self,
      table_name: str,
      key: dict,
      condition_expression: Optional[str] = None,
      *,
      expression_attribute_names: Optional[Dict[str, str]] = None,
      expression_attribute_values: Optional[Dict[str, Any]] = None,
  ):
    """Delete an item from DynamoDB."""
    refuse_inside_transaction("DynamoClient.delete")
    # Prepare delete parameters
    delete_params: Dict[str, Any] = {"Key": key}
    _add_condition(
        delete_params, condition_expression, expression_attribute_names, expression_attribute_values
    )

    # Run blocking operation in thread pool
    await self._run_in_executor(
        lambda: self.get_table(table_name).delete_item(**delete_params)
    )

  async def transact_write_items(
      self,
      transact_items: List[Dict[str, Any]],
      *,
      client_request_token: Optional[str] = None,
  ) -> dict:
    """Send low-level TransactItems as they are, in one TransactWriteItems on this destination. A
    send whose answer is lost is resent with the same ClientRequestToken, so it applies at most
    once; pass the token of an earlier send to settle that one (TransactionOutcomeUnknownError)."""
    refuse_inside_transaction("DynamoClient.transact_write_items")
    return await send_transaction(
        self,
        list(transact_items),
        client_request_token=client_request_token or str(uuid.uuid4()),
        max_attempts=3,
        deadline_seconds=60.0,
    )

  async def run_transaction(
      self,
      fn: Callable[[OptimisticTransaction], Awaitable[R]],
      *,
      max_attempts: int = 8,
      max_commit_attempts: int = 3,
      commit_deadline_seconds: float = 60.0,
  ) -> R:
    """Run `fn(tx)` as one optimistic transaction and return what it returns (transaction.py).

    `fn` reads with `await tx.get(...)` and stages writes with `tx.put/insert/delete/check(...)`;
    they commit together when it returns, or not at all. A lost race reruns `fn` from the start,
    up to `max_attempts` times in all, so `fn` must not do anything a rerun would repeat. A commit
    whose answer is lost is resent with its own token, `max_commit_attempts` sends within
    `commit_deadline_seconds` at most. Called while another runs in the same task, it joins that
    one; see docs/aws.md for the whole contract."""
    return await transaction.run(
        self,
        fn,
        max_attempts=max_attempts,
        max_commit_attempts=max_commit_attempts,
        commit_deadline_seconds=commit_deadline_seconds,
    )

  async def _consistent_get(self, table_name: str, key: Mapping[str, Any]) -> Optional[dict]:
    response = await self._call(
        "get_item", TableName=table_name, Key=serialize_item(key), ConsistentRead=True
    )
    item = response.get("Item")
    return None if item is None else deserialize_item(item)

  async def _call(self, operation: str, **params: Any) -> Any:
    """A low-level client call on the thread pool, where the client is also made on first use."""
    return await self._run_in_executor(lambda: getattr(self.client, operation)(**params))

  async def _run_in_executor(self, func):
    """Run a blocking function in the thread pool executor."""
    return await asyncio.get_running_loop().run_in_executor(executor, func)

  @asynccontextmanager
  async def transaction(self):
    """Context manager for creating a transaction."""
    transaction = DynamoTransaction(self)
    try:
      yield transaction
      await transaction.execute()
    except Exception as e:
      # Transaction failed
      raise e
