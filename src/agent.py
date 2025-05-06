import json
import logging
from pathlib import Path

import openai
from dotenv import set_key, unset_key

# logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class Agent(object):
  assistant_id: str

  def __init__(self, assistant_id: str):
    self.assistant_id = assistant_id

  def delete(self) -> None:
    """
    Delete the gameplay assistant.
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
