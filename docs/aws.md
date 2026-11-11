# devkit.aws: DynamoDB and S3

What the helpers guarantee, what they do not, and what an application still owns. The code is in
`src/aws/`; the tests in `tests/aws/` run on moto, and on DynamoDB Local when one is running
(`tests/aws/conftest.py`).

## Destinations and client reuse

A destination is a profile, a region and an endpoint, each optional. `None` leaves the choice to
boto3: `AWS_PROFILE`, then the region from `AWS_DEFAULT_REGION` or the profile, then the public
endpoint (`AWS_ENDPOINT_URL_DYNAMODB` and the like still apply).

```python
DynamoClient()                                   # the default destination
DynamoClient("prod", region_name="eu-west-1")    # profile and region
S3Client("uploads-staging", region_name="eu-west-1")
```

- Clients for the same destination share one boto3 client, made on first use under a lock. Making
  a `DynamoClient` or `S3Client` per call is cheap.
- Different destinations never share a client. Each `S3Client` keeps its own bucket, so a second
  bucket or profile is never served by the first one, as it was when both classes were process
  singletons.
- boto3 resources are not thread-safe, so each worker thread gets its own resource (the Table-style
  calls). Every network call, S3 body read and listing page runs on a thread pool, never on the
  event loop.
- `reset_clients()` forgets them all: between tests, or after credentials or profiles change.
- No credential is taken as an argument, kept on an object or put in a repr.

## DynamoDB

### Reads

- `DynamoModel.get(pk, sk, consistent_read=True)` and `DynamoClient.get(..., consistent_read=True)`
  read with ConsistentRead. The default stays eventually consistent, as before.
- `query` and `scan`, on the client and the model, return **one page**: at most `Limit` items or
  1 MB, with `LastEvaluatedKey` on the client's response. `query_all` follows every page, and takes
  `max_items`; `Limit` is then the page size.
- Global secondary indexes are never strongly consistent (a DynamoDB limit). Use them to find
  keys, then read each key.

### Conditions

`put`, `update` and `delete`, on the client, the model and the builder transaction, take
`condition_expression` with its own `expression_attribute_names` and `expression_attribute_values`
(plain Python values). The SET expression `update` builds uses `#updN`/`:updN`; a caller placeholder
bound to something else under one of those names is refused before sending.

### Optimistic transactions: `run_transaction`

```python
async def debit(tx):
  account = await tx.get(ACCOUNTS, {"pk": user_id, "sk": "ACCOUNT"})
  if account is None or account["balance"] < amount:
    raise InsufficientFunds()                    # nothing is written; not retried
  tx.put(ACCOUNTS, {**account, "balance": account["balance"] - amount})
  tx.insert(LEDGER, {"pk": user_id, "sk": f"debit#{payment_id}", "amount": amount})

await DynamoClient().run_transaction(debit)
```

`ACCOUNTS` is a `DynamoConfig`, or a `DynamoModel` class. It names the table, its key fields and
`version_field` (default `"version"`).

- **Reads.** `tx.get` is a strongly consistent GetItem. It records the item's version, or that it
  was absent, and returns this transaction's view, its own staged writes included. `tx.query` reads
  every page of a base-table query consistently. Its items may be written, guarded by the version
  it saw, but they are not checked when only read (below). Reads made outside `tx` are not guarded.
- **Writes** are staged and sent in one TransactWriteItems when `fn` returns, or not at all.
  - `put` writes a whole item that the transaction read first.
  - `insert` creates an item that must be absent at commit, with no read needed.
  - `delete` removes an item that was read.
  - `check` requires a condition of an item without writing it.

  Each written item carries `version = read version + 1` (1 when new or unversioned) under the
  guard `version = <read version>`, or `attribute_not_exists(<pk>)` when it was absent. An item that
  exists without a version is guarded by `attribute_exists(<pk>) AND attribute_not_exists(version)`.
  The last write to a key wins, and every condition given for it must hold.
- **Read-only guards.** An item read with `tx.get` but not written gets a ConditionCheck with the
  same guard, so a decision made from it holds when the writes land. A rule decided from a set of
  items (a query) also needs an item every change to that set writes, read with `get`. Items that
  appear in the set (phantoms) are not otherwise detected.
- **A lost race reruns.** If a guard no longer holds (seen through `ALL_OLD` on the cancellation),
  or DynamoDB reports `TransactionConflict` or throttling for an action, nothing was applied. `fn`
  then runs again from the start on fresh reads, with jittered backoff, up to `max_attempts` (8) in
  all, and `TransactionConflictError` is raised after the last. `fn` must therefore do nothing a
  rerun would repeat badly.
- **A lost answer is resent, never rerun.** A commit that timed out, failed with a 5xx or a dropped
  connection, or met `TransactionInProgressException` or throttling may have landed. The same
  request is resent with the **same ClientRequestToken**, up to `max_commit_attempts` (3) sends
  within `commit_deadline_seconds` (60 s). Within DynamoDB's 10-minute token window that applies it
  at most once; the client's 5 s connect and 10 s read timeouts keep every resend inside the window.
  If no send gets an answer, `TransactionOutcomeUnknownError` carries the token and the items.
  Resending them with `DynamoClient.transact_write_items(items, client_request_token=token)` within
  ten minutes settles the outcome; running `fn` again under a new token could apply it twice.
- **Failures that are raised, not retried.**
  - `ConditionFailedError` when a condition the caller gave fails while every guard still held.
  - `TransactionCanceledError` for `ValidationError`, `ItemCollectionSizeLimitExceeded` or an
    unknown reason.
  - Any exception `fn` raises.
  - botocore's own `ClientError` for a definite answer such as `ValidationException`,
    `AccessDeniedException`, `ResourceNotFoundException` or `IdempotentParameterMismatchException`.

  Where a commit lost a race and also failed a condition, the race wins and `fn` reruns: the
  condition may have been decided from what changed.
- **Nesting.** `run_transaction` called while another runs in the same task joins it: one commit,
  and the outer reruns the inner. If the inner raises, its staged writes are undone, its reads stay
  guarded, and the exception reaches the outer. Joining from another destination, or two nested
  calls running concurrently, raises `TransactionUsageError`. So does a write through the original
  API (`put`, `update`, `delete`, `transact_write_items`, a builder transaction,
  `batch_delete_partition`) while `fn` runs: it would not be part of the commit, and a rerun would
  repeat it.
- **Limits.**
  - At most 100 actions per transaction, ConditionChecks included; more is refused before sending.
  - One action per item, which the staging guarantees.
  - DynamoDB refuses items over 400 KB and transactions over 4 MB with a `ValidationException` or
    a `ValidationError` reason, raised as above.
  - All tables must be in one account and region: that of the client.
  - Each action costs twice a plain write's capacity (DynamoDB bills transactions twice), and a
    ConditionCheck costs a write.
  - A transaction that only reads commits nothing, so its reads are each consistent but are not a
    snapshot.
- **Models.**
  - `tx.get_model(Model, pk, sk)` reads through `tx.get` and validates with the model.
  - `put_model`, `insert_model` and `delete_model` stage the model's stored fields (relations
    excluded) and clear its dirty fields on commit.
  - `Model.from_item` drops the version attribute unless the model declares it, so models with
    `extra="forbid"` still load versioned items.

### Builder transactions: `DynamoClient.transaction()`

The original API: fixed writes, each under its own condition, sent in one TransactWriteItems when
the block ends. Nothing is read, guarded or rerun, and a cancellation is raised as botocore's
`TransactionCanceledException`, as before. It now sends to the client's own destination (it used a
default `boto3.client("dynamodb")`). It types booleans, sets and bytes correctly (a `bool` used to
go out as a number). It takes placeholders, offers `condition_check`, and resends a lost answer with
the same token.

## S3

- **Bytes.** `put_bytes(key, data, content_type=..., sha256=..., metadata=...)` is one PutObject
  with `ChecksumSHA256`, which S3 verifies on receipt and keeps with the object. A `sha256` (hex)
  given by the caller is checked against the data before sending. `get_bytes(key,
  expected_sha256=...)` reads the body on the thread pool with `ChecksumMode=ENABLED`, so botocore
  checks the stored checksum. It raises `S3IntegrityError` on a mismatch with either checksum.
  Payloads are held in memory and sent in one PUT: no multipart.
- **Text.** `upload_document`, `download_document`, `delete_document` and `document_exists` keep
  their signatures and requests.
- **Not found is told apart from refused.**
  - `get_bytes` and `download_document` raise `S3ObjectNotFoundError` for a missing key. It is a
    `ClientError` subclass carrying S3's own response, so callers that catch `ClientError` and read
    `NoSuchKey` keep working.
  - `head_object` returns `None` and `object_exists`/`document_exists` return `False` only for a
    missing key. AccessDenied, a missing bucket on GET, or a 5xx is raised; `document_exists` used
    to return `False` for those.
  - Without `s3:ListBucket`, S3 answers a missing key with 403, which raises. A HEAD response has
    no body, so a missing bucket reads as a missing key there.
- **Listing and deleting.** `iter_objects(prefix, page_size=1000)` yields every object, a page at a
  time. `delete_folder(prefix)` deletes them 1000 at a time. If S3 refuses some keys inside a 200
  answer, it raises `S3DeleteError` with S3's entries, where it used to count them as deleted. On a
  versioned bucket it adds delete markers.
- **Presigned downloads.** `presigned_get_url(key, expires_in=..., filename=..., content_type=...)`
  signs with SigV4 (the client is configured `s3v4`; botocore otherwise presigns some regions with
  SigV2, which newer buckets refuse).
  - `expires_in` runs from 1 s to 7 days; temporary credentials expire the URL sooner.
  - `filename` becomes an attachment Content-Disposition, with an ASCII fallback and the exact
    name in RFC 5987 form.
  - Signing checks neither that the key exists nor that the caller may read it. The URL is a
    bearer credential: do not log it.
  - The client's region must be the bucket's.

## DynamoDB and S3 are not one transaction

Nothing here makes an S3 write and a DynamoDB commit atomic, and no API suggests it. An application
that keeps payloads in S3 for its items owns:

- writing the object, under a new key, before the transaction that names it;
- deleting it after a transaction that stops naming it, and when that transaction is abandoned;
- sweeping keys that no item names (a crash between the two steps).

A rerun of `fn` repeats any S3 write `fn` makes. Compression, inline-versus-bucket thresholds and
orphan sweeps stay in the application, as InterviewSoon's `lib/platform/data.py` keeps them.

## Python and dependencies

- **Python 3.12 and 3.13.** `requires-python` is `>=3.12`. The suite passes on both, and on the
  library versions DevKit's consumers lock:
  - prdhub: boto3/botocore 1.38.44, pydantic 2.11.7;
  - InterviewSoon: Python 3.12, boto3/botocore 1.43.107, pydantic 2.13.5, moto 5.2.3.

  Nothing in `src/` needed 3.13.
- **pydantic.** The exact `pydantic==2.11.2` pin, which could not be installed beside
  InterviewSoon's 2.13.5, is now `>=2.11.2,<3`.
- **Base dependencies.** They still include `openai`, `pyjwt`, `pyyaml` and `python-dotenv`, which
  other DevKit modules use. An application installing DevKit for `devkit.aws` gets them too;
  splitting them into extras is future work.

## Capability matrix

| Capability | Status | Notes |
|---|---|---|
| Strongly consistent keyed reads | Implemented | `tx.get`; `get(..., consistent_read=True)` on the client and model |
| Version and absence guards on writes | Implemented | `run_transaction`; `version_field` per table, integer counter from 1 |
| Read-only condition checks | Implemented | ConditionCheck for every item read by key and not written; `tx.check` for given conditions |
| Atomic multi-item commit | Implemented | One TransactWriteItems, at most 100 actions, one region and account |
| Rerun on a lost race, bounded | Implemented | Fresh reads each attempt, jittered backoff, `TransactionConflictError` after `max_attempts` |
| Same-token resend of an uncertain commit | Implemented | `TransactionOutcomeUnknownError` carries token and items; never reruns `fn` |
| Terminal failures surfaced, not retried | Implemented | Condition failure, validation, access and unknown cancellations |
| Nested transactions | Implemented | Join in the same task; inner failure undoes its writes and keeps its reads guarded |
| Conditions with their own placeholders | Implemented | Client, model and builder; collisions refused |
| Full query pagination | Implemented | `query_all` and `tx.query`; `query` and `scan` stay one page |
| Predictable destinations, thread and event-loop safety | Implemented | Client per destination, resource per thread, all I/O on the pool |
| S3 bytes with SHA-256, presigned GET, not-found apart from refusal | Implemented | One PUT per payload, in memory |
| Python 3.12 | Implemented | Tested on 3.12 and 3.13 |
| Phantom protection for query-based rules | Not provided | Needs an aggregate item written by every change to the set, read with `get` |
| Writers that skip the version | Not detected | A plain `update` leaves the version alone, so guards cannot see it; a plain `put` drops it (guards see that) and the item restarts at 1. Route every write of a versioned item through `run_transaction` |
| Delete then re-create inside one read-to-commit window | Not detected | The new item restarts at version 1 and can satisfy an old guard (ABA), as with any counter version; keys that are never reused avoid it |
| Snapshot reads (TransactGetItems) | Not implemented | A read-only `fn` commits nothing |
| In-memory backend with the same contract | Not implemented | Tests use moto and DynamoDB Local; InterviewSoon's twin stays its own |
| Cancelling `run_transaction` mid-commit | Outcome unknown | The send runs to its end on the pool |
| S3 multipart, streaming, compression, inline thresholds, orphan sweep | Not implemented | Application responsibilities |
| DynamoDB plus S3 atomicity | Not provided | See above |
| InterviewSoon integration | Not started | Out of scope here; its data layer, records and runtime are unchanged |

## Tests

```sh
uv run --extra dev pytest tests/aws             # moto only; the DynamoDB Local tests skip

docker run -d --rm -p 127.0.0.1:8799:8000 amazon/dynamodb-local:2.5.2 -jar DynamoDBLocal.jar -inMemory -sharedDb
export DEVKIT_TEST_DYNAMODB_LOCAL=http://127.0.0.1:8799
uv run --extra dev pytest tests/aws
UV_PROJECT_ENVIRONMENT=.venv-py312 uv run --extra dev --python 3.12 pytest tests/aws
```

The race tests that need commits atomic across threads run only on DynamoDB Local. moto ignores
`ClientRequestToken`, so the resend tests answer a replayed token there as DynamoDB does. On
DynamoDB Local the token is DynamoDB's own.
