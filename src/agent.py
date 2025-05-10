import logging
from typing import Dict

import openai
from pydantic import BaseModel

from .yaml import parse_file

# logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class Agent(object):
  _config: Dict
  _schema: BaseModel

  def __init__(self, config_path: str, schema: BaseModel):
    self._config = parse_file(config_path)
    self._schema = schema
    self._client = openai.OpenAI()

  def query(self, prompt: str) -> Dict[str, str]:
    """
    Sends a prompt to the OpenAI API and returns the response.

    :param prompt: The user's prompt (either an exact command or an instructional request).
    :return: The response in JSON format.
    """
    logger.debug(f"Querying OpenAI API with prompt: {prompt}")

    params = dict(self._config)
    params['messages'].append({"role": "user", "content": prompt})
    params['response_format'] = self._schema

    try:
        # Using the correct method for chat completions (openai.chat.completions.create)
      logger.info(f"Sending request to OpenAI API ({params['model']})")
      completion = self._client.beta.chat.completions.parse(**params)

      # Parse the response and return the result
      result = completion.choices[0].message.parsed
      logger.info(f"Response: {result}")
      return result
    except Exception as e:
      logger.error(f"Error querying the OpenAI API: {e}")
      raise
