import json
import logging
import hashlib
import os

import openai
from pydantic import BaseModel
from dotenv import set_key, unset_key

from .yaml_utils import parse_file

# logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class Assistant(object):
  config: dict
  model: BaseModel
  env_var: str

  def __init__(self, config_path: str, model: BaseModel, env_var: str = "ASSISTANT_ID"):
    self.env_var = env_var
    self.model = model

    # Load the assistant config
    self.config = parse_file(config_path)
    self.config["response_format"] = {
        "type": "json_schema",
        "json_schema": {
            "name": "schema",
                "description": "A schema for the response.",
                "schema": model.model_json_schema(),
        }
    }

  def _compute_fingerprint(self, config: dict) -> str:
    """
    Compute a fingerprint of the configuration.
    """
    try:
      del config["metadata"]
    except KeyError:
      pass

    config_str = json.dumps(config, sort_keys=True)
    return hashlib.sha256(config_str.encode()).hexdigest()

  def create(self) -> None:
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
      logger.error(f"Error creating search assistant: {e}")
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

  def _get_remote_config(self) -> openai.types.beta.assistant.Assistant:
    """
    Fetch the current assistant configuration from OpenAI API.
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
    try:
      assistant_id = self._get_assistant_id()
      openai.beta.assistants.delete(assistant_id)
      logger.info(f"Assistant deleted: {assistant_id}")
    except Exception as e:
      logger.error(f"Error deleting assistant: {e}")
      raise

    # Remove the assistant ID from .env file
    unset_key(".env", self.env_var)

  def query(self, prompt: str) -> str:
    '''
    Query the assistant.
    '''
    logger.info("Querying the assistant")

    try:
      # Create a thread
      thread = openai.beta.threads.create()
      logger.info(f"Created thread: {thread.id}")

      # Create a message
      message = openai.beta.threads.messages.create(
          thread_id=thread.id,
          role="user",
          content=prompt
      )
      logger.info(f"Created message: {message.id}")

      # Create a run and wait for completion
      assistant_id = self._get_assistant_id()
      run = openai.beta.threads.runs.create_and_poll(thread_id=thread.id, assistant_id=assistant_id)

      if run.status != "completed":
        raise Exception(f"Run ended with status: {run.status}")

      logger.info("Run completed successfully")

      # Retrieve latest message from the thread
      messages = openai.beta.threads.messages.list(
          thread_id=thread.id,
          limit=1
      )

      # Process the answers
      response = messages.data[0].content[0].text.value
    except Exception as e:
      logger.error(f"Error in query handling: {e}")
      raise

    parsed = json.loads(response)
    logger.info(f"Response:\n{json.dumps(parsed, indent=2)}")
    return self.model.model_validate(parsed)

  def _get_assistant_id(self) -> str:
    """
    Get the assistant ID from environment variables.
    Raises ValueError if not found.
    """
    assistant_id = os.getenv(self.env_var)
    if not assistant_id:
      raise ValueError(f"Assistant ID not found in environment variable {self.env_var}")
    return assistant_id
