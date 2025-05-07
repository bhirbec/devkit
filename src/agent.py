import openai
import json
import yaml
import logging
from typing import Dict
from pydantic import BaseModel

# logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class Agent(object):
  _config: Dict
  _schema: BaseModel

  def __init__(self, config_path: str, schema: BaseModel):
    self._config = self._load_config(config_path)
    self._schema = schema
    self._client = openai.OpenAI()

  def _load_config(self, config_path: str) -> Dict:
    """
    Loads the assistant's instructions from a markdown file.
    """
    try:
      with open(config_path, 'r') as file:
        config = yaml.safe_load(file)
      return config
    except Exception as e:
      print(f"Error loading configuration: {e}")
      raise

  def query(self, prompt: str) -> Dict[str, str]:
    """
    Sends a prompt to the OpenAI API and returns the response.

    :param prompt: The user's prompt (either an exact command or an instructional request).
    :return: The response in JSON format.
    """

    params = dict(self._config)
    params['messages'].append({"role": "user", "content": prompt})

    # Set the response format as specified in the Pydantic schema
    params['response_format'] = self._schema

    try:
        # Using the correct method for chat completions (openai.chat.completions.create)
      completion = self._client.beta.chat.completions.parse(**params)

      # Parse the response and return the result
      result = completion.choices[0].message.parsed
      logger.info(f"Response: {result}")
      return result
    except Exception as e:
      logger.error(f"Error querying the OpenAI API: {e}")
      raise
