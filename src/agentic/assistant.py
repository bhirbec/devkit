import json
import logging
import hashlib
import os
from typing import Any, Type

import openai
from pydantic import BaseModel
from dotenv import set_key, unset_key

from pykit.agentic.thread import Thread

# logging
logger = logging.getLogger(__name__)


class Assistant(object):
  config: dict
  model: Type[BaseModel]
  env_var: str

  def __init__(self, config: dict, model: Type[BaseModel], env_var: str = "ASSISTANT_ID"):
    self.env_var = env_var
    self.model = model
    self.config = config

  def create_thread(self) -> Thread:
    """
    Create a new thread for conversation with the assistant.

    Returns:
      A Thread object
    """
    try:
      thread = openai.beta.threads.create()
      logger.info(f"Created thread: {thread.id}")
      return Thread(thread.id, self._get_assistant_id(), self.model)
    except Exception as e:
      logger.error(f"Error creating thread: {e}")
      raise

  def init_thread(self, thread_id: str) -> Thread:
    """
    Initialize a thread for conversation with the assistant.

    Returns:
      A Thread object
    """
    return Thread(thread_id, self._get_assistant_id(), self.model)

  def _compute_fingerprint(self, config: dict) -> str:
    """
    Compute a fingerprint of the configuration.
    """
    # Create a copy of the config to avoid modifying the original
    config_copy = config.copy()

    try:
      # Remove metadata if it exists
      del config_copy["metadata"]
    except KeyError:
      pass

    config_str = json.dumps(config_copy, sort_keys=True)
    return hashlib.sha256(config_str.encode()).hexdigest()

  def create(self) -> None:
    """
    Create a new assistant.
    """
    try:
      # Add fingerprint to metadata
      fingerprint = self._compute_fingerprint(self.config)

      assistant = openai.beta.assistants.create(
          **self.config,
          metadata={"fingerprint": fingerprint}
      )
      logger.info(f"Assistant created: {assistant.id}")

      # Save the assistant ID to .env file
      set_key(".env", self.env_var, assistant.id)
    except Exception as e:
      logger.error(f"Error creating assistant: {e}")
      raise

  def update(self) -> None:
    """
    Update the assistant configuration.
    """
    try:
      assistant_id = self._get_assistant_id()
      assistant = openai.beta.assistants.update(assistant_id=assistant_id, **self.config)
      logger.info(f"Assistant updated: {assistant.id}")
    except Exception as e:
      logger.error(f"Error updating assistant: {e}")
      raise

  def update_if_config_has_changed(self) -> None:
    """
    Update the assistant if configuration fingerprint differs from remote.
    """
    fingerprint = self._compute_fingerprint(self.config)
    remote = self._get_remote_config()
    remote_fingerprint = remote.metadata.get("fingerprint")

    if fingerprint != remote_fingerprint:
      logger.info("Configuration has changed, updating assistant...")
      self.update()
    else:
      logger.info("No changes detected in configuration.")

  def _get_remote_config(self) -> Any:
    """
    Fetch the current assistant configuration from OpenAI API.

    Returns:
      The assistant configuration from OpenAI
    """
    try:
      assistant_id = self._get_assistant_id()
      return openai.beta.assistants.retrieve(assistant_id)
    except Exception as e:
      logger.error(f"Error fetching assistant: {e}")
      raise

  def delete(self) -> None:
    """
    Delete the assistant.
    """
    assistant_id = self._get_assistant_id()
    try:
      openai.beta.assistants.delete(assistant_id)
      logger.info(f"Assistant deleted: {assistant_id}")

      # Remove the assistant ID from .env file
      unset_key(".env", self.env_var)
    except Exception as e:
      logger.error(f"Error deleting assistant {assistant_id}: {e}")
      raise

  def query(self, prompt: str) -> Any:
    '''
    Query the assistant with a single prompt and return the result.

    This is a convenience method that creates a thread, adds a message,
    runs the thread, and returns the result.

    Args:
      prompt: The user's prompt

    Returns:
      The validated response
    '''
    logger.info(f"Querying the assistant with prompt: {prompt[:50]}...")

    try:
      # Create a thread and use it for the query
      thread = self.create_thread()
      message_id = thread.add_message(prompt)
      logger.debug(f"Added message with ID: {message_id}")

      result = thread.run()
      return result
    except Exception as e:
      logger.error(f"Error in query handling: {e}")
      raise

  def _get_assistant_id(self) -> str:
    """
    Get the assistant ID from environment variables.
    Raises ValueError if not found.
    """
    assistant_id = os.getenv(self.env_var)
    if not assistant_id:
      raise ValueError(f"Assistant ID not found in environment variable {self.env_var}")
    return assistant_id
