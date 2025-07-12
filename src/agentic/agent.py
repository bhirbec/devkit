import logging
from typing import Dict

import openai
from pydantic import BaseModel

from pykit.templating.yaml import parse_file

# logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


DEFAULT_PARAMS = {
    "model": "gpt-4o-mini-2024-07-18",
    "messages": [],
}


class Agent(object):
  _config: Dict
  _schema: BaseModel

  def __init__(self, config_path: str, schema: BaseModel):
    self._config = parse_file(config_path)
    self._schema = schema

  def query(self, prompt: str) -> Dict[str, str]:
    """
    Sends a prompt to the OpenAI API and returns the response.

    :param prompt: The user's prompt (either an exact command or an instructional request).
    :return: The response in JSON format.
    """
    params = dict(DEFAULT_PARAMS)
    params.update(self._config)
    params['messages'].append({"role": "user", "content": prompt})
    params['response_format'] = self._schema

    client = openai.OpenAI()

    try:
      logger.info(f"Querying OpenAI API")
      logger.debug(f"client.beta.chat.completions.parse params : {params}")
      completion = client.beta.chat.completions.parse(**params)

      # Parse the response and return the result
      result = completion.choices[0].message.parsed
      logger.info(f"Response:\n{result}")
      return result
    except Exception as e:
      logger.error(f"Error querying the OpenAI API: {e}")
      raise
