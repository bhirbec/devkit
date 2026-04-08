import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional, Dict, List, ClassVar, Type, TypeVar, Generic, cast, TypedDict, Set

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config
from contextlib import asynccontextmanager
from pydantic import BaseModel, Field, ValidationError

# Define TypeVars for models that inherit from BaseModel
T = TypeVar("T", bound=BaseModel)
# Self type for the DynamoModel class
Self = TypeVar("Self", bound="DynamoModel")


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


class DynamoConfig:
  """Configuration for DynamoDB table."""

  def __init__(self, table_name: str, pk_field: str, sk_field: Optional[str] = None, relations: Optional[List[str]] = None):
    self.table_name = table_name
    self.pk_field = pk_field
    self.sk_field = sk_field
    self.relations = relations or []


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
  async def get(cls: Type[Self], pk_value: str, sk_value: Optional[str] = None) -> Optional[Self]:
    """
    Retrieve an item by primary key and optional sort key.

    Args:
        pk_value: The value of the partition key
        sk_value: The value of the sort key (if the table has a sort key)

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
          key
      )
      if not item:
        return None
      try:
        return cls(**item)
      except ValidationError as exc:
        pk_field = cls._dynamo_config.pk_field
        sk_field = cls._dynamo_config.sk_field
        sk_value_str = f", {sk_field}={sk_value}" if sk_field and sk_value else ""
        raise ValueError(
            f"Invalid record for {cls.__name__} {pk_field}={pk_value}{sk_value_str}"
        ) from exc

  @classmethod
  async def query(cls: Type[Self], **kwargs) -> List[Self]:
    """Query items based on provided parameters."""
    async with DynamoClient() as dynamodb:
      response = await dynamodb.query(cls._dynamo_config.table_name, **kwargs)
      items = response.get("Items", [])
      return [cls(**item) for item in items]

  async def put(self, condition_expression: Optional[str] = None) -> None:
    """
    Put the model instance into DynamoDB.
    Uses transaction.put() internally to maintain consistency with transaction operations.
    """
    async with DynamoClient() as dynamodb:
      # Use put with optional condition
      await dynamodb.put(
          self.__class__._dynamo_config.table_name,
          self._model_dump_without_relations(),
          condition_expression
      )

    # Clear dirty fields after successful delete
    self.clear_dirty_fields()

  async def update(self, condition_expression: Optional[str] = None) -> None:
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
          condition_expression=condition_expression
      )

    # Clear dirty fields after successful update
    self.clear_dirty_fields()

  async def delete(self, condition_expression: Optional[str] = None) -> None:
    """
    Delete the model instance from DynamoDB.
    """
    async with DynamoClient() as dynamodb:
      # Use delete with optional condition
      await dynamodb.delete(
          self.__class__._dynamo_config.table_name,
          self.primary_key,
          condition_expression
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
    async with DynamoClient() as dynamodb:
      table = dynamodb.get_table(cls._dynamo_config.table_name)

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

        resp = await dynamodb.query(cls._dynamo_config.table_name, **query_params)
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

        # Synchronous batch_writer with internal 25-item batching + retries
        def _delete_page():
          with table.batch_writer() as batch:
            for dk in page_keys:
              batch.delete_item(Key=dk)

        await dynamodb._run_in_executor(_delete_page)
        deleted += len(page_keys)

        start_key = resp.get("LastEvaluatedKey")
        if not start_key:
          break

      return {"deleted_count": deleted, "unprocessed_keys": []}


class DynamoTransaction:
  """Context manager for DynamoDB transactions."""

  def __init__(self, client):
    self._statements = []
    self._models = []  # Track models used in this transaction
    self._dynamodb_client = boto3.client("dynamodb")
    self._client = client

  def put(self, model: DynamoModel, condition_expression: Optional[str] = None) -> None:
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

    # Add condition expression if provided
    if condition_expression:
      put_dict["ConditionExpression"] = cast(Any, condition_expression)

    # Create the complete statement and track the model
    self._statements.append({"Put": put_dict})
    self._models.append(model)

  def update(self, model: DynamoModel, condition_expression: Optional[str] = None) -> None:
    """
    Create an Update statement for a transaction using dirty fields and add it to the transaction.
    """
    dirty_fields = model.get_dirty_fields()
    if not dirty_fields:
      raise ValueError("Model does not have any fields to update")

    # Serialize the model to properly handle nested objects, then extract only dirty fields
    serialized_data = model._model_dump_without_relations()
    serialized_updates = {f: serialized_data[f] for f in dirty_fields}

    # Build update expression
    update_expr_parts = []
    expr_attr_names = {}
    expr_attr_values = {}

    for i, (field_name, value) in enumerate(serialized_updates.items()):
      placeholder_name = f"#k{i}"
      placeholder_value = f":v{i}"
      update_expr_parts.append(f"{placeholder_name} = {placeholder_value}")
      expr_attr_names[placeholder_name] = field_name
      expr_attr_values[placeholder_value] = value

    update_expression = "SET " + ", ".join(update_expr_parts)

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

    # Add condition expression if provided
    if condition_expression:
      update_dict["ConditionExpression"] = cast(Any, condition_expression)

    # Create the complete statement and track the model
    self._statements.append({"Update": update_dict})
    self._models.append(model)

  def delete(self, model: DynamoModel, condition_expression: Optional[str] = None) -> None:
    """
    Create a Delete statement for a transaction and add it to the transaction.
    """
    # Create the base delete dictionary
    delete_dict: DynamoDBOperationDict = {
        "TableName": model.__class__._dynamo_config.table_name,
        "Key": self._formatted_primary_key(model),
    }

    # Add condition expression if provided
    if condition_expression:
      delete_dict["ConditionExpression"] = cast(Any, condition_expression)

    # Create the complete statement and track the model
    self._statements.append({"Delete": delete_dict})
    self._models.append(model)

  async def execute(self) -> None:
    """Execute the transaction."""
    if not self._statements:
      return  # No statements to execute

    try:
      # Run in executor to avoid blocking
      await self._client._run_in_executor(
          lambda: self._dynamodb_client.transact_write_items(TransactItems=self._statements)
      )
    except Exception as e:
      raise e
    else:
      self._clear_dirty_fields()
    finally:
      self._reset()

  def to_dynamodb_value(self, value: Any) -> Dict[str, Any]:
    """Convert a Python value to a DynamoDB-typed value."""
    if isinstance(value, str):
      return {"S": value}
    elif isinstance(value, (int, float, Decimal)):
      return {"N": str(value)}
    elif isinstance(value, dict):
      return {"M": {k: self.to_dynamodb_value(v) for k, v in value.items()}}
    elif isinstance(value, list):
      return {"L": [self.to_dynamodb_value(v) for v in value]}
    elif isinstance(value, bool):
      return {"BOOL": value}
    elif value is None:
      return {"NULL": True}
    else:
      raise ValueError(f"Unsupported value type: {type(value)}")

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
  _instance = None
  _dynamodb: Any = None

  def __new__(cls, profile_name: str = ""):
    if cls._instance is None:
      cls._instance = super(DynamoClient, cls).__new__(cls)
      # Configure connection pooling
      config = Config(
          max_pool_connections=50,  # Adjust based on workload
          retries={"max_attempts": 3, "mode": "standard"}
      )

      # Initialize the DynamoDB resource with optional profile
      if profile_name:
        session = boto3.Session(profile_name=profile_name)
        cls._dynamodb = session.resource("dynamodb", config=config)
      else:
        cls._dynamodb = boto3.resource("dynamodb", config=config)
    return cls._instance

  async def __aenter__(self):
    # Reuse existing connection
    return self

  async def __aexit__(self, exc_type, exc, tb):
    # No need to close connection, it's reused
    pass

  def get_table(self, table_name: str):
    return self._dynamodb.Table(table_name)

  async def get(self, table_name: str, key: dict):
    table = self.get_table(table_name)
    # Run blocking operation in thread pool
    response = await self._run_in_executor(
        lambda: table.get_item(Key=key)
    )
    return response.get("Item")

  async def query(self, table_name: str, **kwargs):
    table = self.get_table(table_name)
    # Run blocking operation in thread pool
    response = await self._run_in_executor(
        lambda: table.query(**kwargs)
    )
    return response

  async def scan(self, table_name: str, **kwargs):
    """Scan all items in a DynamoDB table."""
    table = self.get_table(table_name)
    # Run blocking operation in thread pool
    response = await self._run_in_executor(
        lambda: table.scan(**kwargs)
    )
    return response

  async def put(self, table_name: str, item: dict, condition_expression: Optional[str] = None):
    table = self.get_table(table_name)

    # Prepare put parameters
    put_params = {"Item": item}
    if condition_expression:
      put_params["ConditionExpression"] = cast(Any, condition_expression)

    # Run blocking operation in thread pool
    await self._run_in_executor(
        lambda: table.put_item(**put_params)
    )

  async def update(
      self,
      table_name: str,
      key: dict,
      updates: Dict[str, Any],
      condition_expression: Optional[str] = None
  ):
    table = self.get_table(table_name)

    update_expr_parts = []
    expr_attr_names = {}
    expr_attr_values = {}

    for i, (field_name, value) in enumerate(updates.items()):
      placeholder_name = f"#k{i}"
      placeholder_value = f":v{i}"
      update_expr_parts.append(f"{placeholder_name} = {placeholder_value}")
      expr_attr_names[placeholder_name] = field_name
      expr_attr_values[placeholder_value] = value

    update_expression = "SET " + ", ".join(update_expr_parts)

    # Prepare update parameters
    update_params = {
        "Key": key,
        "UpdateExpression": update_expression,
        "ExpressionAttributeNames": expr_attr_names,
        "ExpressionAttributeValues": expr_attr_values,
    }

    if condition_expression:
      update_params["ConditionExpression"] = cast(Any, condition_expression)

    await self._run_in_executor(
        lambda: table.update_item(**update_params)
    )

  async def delete(self, table_name: str, key: dict, condition_expression: Optional[str] = None):
    """Delete an item from DynamoDB."""
    table = self.get_table(table_name)

    # Prepare delete parameters
    delete_params = {"Key": key}
    if condition_expression:
      delete_params["ConditionExpression"] = cast(Any, condition_expression)

    # Run blocking operation in thread pool
    await self._run_in_executor(
        lambda: table.delete_item(**delete_params)
    )

  async def transact_write_items(self, transact_items: List[Dict[str, Any]]):
    """Execute a transaction with multiple write operations."""
    dynamodb_client = boto3.client("dynamodb")

    # Run blocking operation in thread pool
    await self._run_in_executor(
        lambda: dynamodb_client.transact_write_items(TransactItems=transact_items)
    )

  async def _run_in_executor(self, func):
    """Run a blocking function in the thread pool executor."""
    return await asyncio.get_event_loop().run_in_executor(executor, func)

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
