"""boto3 clients shared per destination: the profile, region and endpoint a client talks to.

A client costs a service-model load and a connection pool, and is thread-safe once made, so each
destination gets one, made on first use. boto3 sessions are not thread-safe while they make clients,
so creation holds a lock. Resources are not thread-safe at all (their DynamoDB condition builder is
shared state), so each thread gets its own.
"""

import threading
from dataclasses import dataclass
from typing import Any, Optional

from boto3.session import Session
from botocore.config import Config


@dataclass(frozen=True)
class AwsTarget:
  """Where a client sends its calls. None leaves the choice to boto3: AWS_PROFILE, the profile's
  region or AWS_DEFAULT_REGION, the service's public endpoint."""

  profile_name: Optional[str] = None
  region_name: Optional[str] = None
  endpoint_url: Optional[str] = None


_lock = threading.Lock()
_sessions: dict[Optional[str], Session] = {}
_clients: dict[tuple[str, AwsTarget], Any] = {}
_local = threading.local()
_generation = 0


def _session(profile_name: Optional[str]) -> Session:
  # Called with _lock held.
  session = _sessions.get(profile_name)
  if session is None:
    session = _sessions[profile_name] = Session(profile_name=profile_name)
  return session


def shared_client(service: str, target: AwsTarget, config: Config) -> Any:
  """The low-level client for `service` at `target`, one per process. Each service passes the
  same `config` every time; it is not part of the cache key."""
  key = (service, target)
  client = _clients.get(key)
  if client is None:
    with _lock:
      client = _clients.get(key)
      if client is None:
        client = _session(target.profile_name).client(
            service,
            region_name=target.region_name,
            endpoint_url=target.endpoint_url,
            config=config,
        )
        _clients[key] = client
  return client


def thread_resource(service: str, target: AwsTarget, config: Config) -> Any:
  """The calling thread's own boto3 resource for `service` at `target`."""
  resources = getattr(_local, "resources", None)
  if resources is None or _local.generation != _generation:
    resources = _local.resources = {}
    _local.generation = _generation
  key = (service, target)
  resource = resources.get(key)
  if resource is None:
    with _lock:
      resource = _session(target.profile_name).resource(
          service,
          region_name=target.region_name,
          endpoint_url=target.endpoint_url,
          config=config,
      )
    resources[key] = resource
  return resource


def reset_clients() -> None:
  """Forget every cached session, client and resource, so the next call reads the environment and
  credentials again: between tests, or after a profile's configuration changed."""
  global _generation
  with _lock:
    _sessions.clear()
    _clients.clear()
    _generation += 1
