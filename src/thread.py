import logging
import json
from typing import Optional, Any, Type, Union

import openai
from pydantic import BaseModel

# logging
logger = logging.getLogger(__name__)


class Thread:
  """
  Represents an OpenAI thread for conversation with an assistant.
  """

  def __init__(self, thread_id: str, assistant_id: str):
    self.thread_id = thread_id
    self.assistant_id = assistant_id

  def add_message(self, content: str) -> str:
    """
    Add a message to the thread.

    Args:
      content: The message content

    Returns:
      The message ID
    """
    try:
      message = openai.beta.threads.messages.create(
          thread_id=self.thread_id,
          role="user",
          content=content
      )
      logger.info(f"Created message: {message.id}")
      return message.id
    except Exception as e:
      logger.error(f"Error creating message: {e}")
      raise

  def get_last_message(self, model: Optional[Type[BaseModel]] = None) -> Any:
    """
    Retrieve the latest message from the thread.

    Args:
      model: Optional Pydantic model to validate the response against

    Returns:
      If model is provided, returns the validated model instance
      Otherwise, returns the raw message content
    """
    messages = openai.beta.threads.messages.list(
        thread_id=self.thread_id,
        limit=1
    )

    if not messages.data:
      raise ValueError("No messages found in the thread")

    content = messages.data[0].content[0]

    # If no model is provided, return the raw content
    if model is None:
      return content

    # Handle different content types based on the content type
    if content.type == 'text':
      response = content.text.value
      try:
        parsed = json.loads(response)
        logger.info(f"Response:\n{json.dumps(parsed, indent=2)}")
        return model.model_validate(parsed)
      except json.JSONDecodeError as e:
        logger.error(f"Error parsing JSON response: {e}")
        logger.error(f"Raw response: {response}")
        raise ValueError(f"Invalid JSON response: {e}")
    else:
      raise ValueError(f"Unexpected content type: {content.type}")

  def run(self, model: Type[BaseModel]) -> Any:
    """
    Start a run on the thread and wait for the result.

    Args:
      model: The Pydantic model to validate the response against

    Returns:
      The validated response
    """
    try:
      run = openai.beta.threads.runs.create_and_poll(
          thread_id=self.thread_id,
          assistant_id=self.assistant_id
      )
      logger.info(f"Started run: {run.id}")

      if run.status != "completed":
        raise Exception(f"Run ended with status: {run.status}")

      logger.info("Run completed successfully")

      # Get the latest message and validate against the model
      return self.get_last_message(model)
    except Exception as e:
      logger.error(f"Error running thread: {e}")
      raise
