import json
import logging
import hashlib
from pathlib import Path

import openai
from dotenv import set_key, unset_key

from .yaml import parse_file


# logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class Agent(object):
  assistant_id: str
  config: dict
  _fingerprint: str

  def __init__(self, config_path, schema, assistant_id: str = None):
    self.assistant_id = assistant_id
    # Load the assistant config
    self.config = parse_file(config_path)

    # Add response format to config
    self.config["response_format"] = {
        "type": "json_schema",
        "json_schema": {
            "name": "schema",
            "description": "A schema for the response.",
            "schema": schema,
        }
    }

    self._fingerprint = self._compute_fingerprint(self.config)
    self.config["metadata"] = {"fingerprint": self._fingerprint}

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
      self.config["metadata"] = {"fingerprint": self._fingerprint}

      assistant = openai.beta.assistants.create(**self.config)
      print(f"Assistant created: {assistant.id}")
      self.assistant_id = assistant.id

      # Save the assistant ID to .env file
      set_key(".env", "ASSISTANT_ID", assistant.id)
    except Exception as e:
      print(f"Error creating search assistant: {e}")
      raise

  def update(self) -> None:
    """
    Update the assistant configuration.
    """
    try:
      assistant = openai.beta.assistants.update(
          assistant_id=self.assistant_id,
          **self.config
      )
      print(f"Assistant updated: {assistant.id}")
    except Exception as e:
      print(f"Error updating assistant: {e}")
      raise

  def update_if_config_has_changed(self) -> None:
    """
    Update the assistant if configuration fingerprint differs from remote.
    """
    remote = self._get_remote_config()
    remote_fingerprint = remote.metadata.get("fingerprint")

    if self._fingerprint != remote_fingerprint:
      print("Configuration has changed, updating assistant...")
      self.update()
    else:
      print("No changes detected in configuration.")

  def _get_remote_config(self) -> openai.types.beta.assistant.Assistant:
    """
    Fetch the current assistant configuration from OpenAI API.
    """
    try:
      return openai.beta.assistants.retrieve(self.assistant_id)
    except Exception as e:
      print(f"Error fetching assistant: {e}")
      raise

  def delete(self) -> None:
    """
    Delete the assistant.
    """
    try:
      openai.beta.assistants.delete(self.assistant_id)
      print(f"Assistant deleted: {self.assistant_id}")
    except Exception as e:
      print(f"Error deleting assistant: {e}")
      raise

    # Remove the assistant ID from .env file
    unset_key(".env", "ASSISTANT_ID")

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
      run = openai.beta.threads.runs.create_and_poll(
          thread_id=thread.id,
          assistant_id=self.assistant_id
      )

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

    # by convention, the response is a JSON object with a "root" key. This is a limitation
    # of the OpenAI API: the root must be a JSON object, not a JSON array.
    parsed = json.loads(response)
    logger.info(f"Response:\n{json.dumps(parsed, indent=2)}")
    return parsed["root"]
