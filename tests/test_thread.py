import os
import pytest

from pydantic import BaseModel
from unittest.mock import patch, MagicMock

from pykit.thread import Thread


@pytest.fixture(autouse=True)
def setup_env():
  os.environ["OPENAI_API_KEY"] = "dummy-key"


class DummyModel(BaseModel):
  name: str
  age: int


class TestThread:

  @patch('openai.beta.threads.messages.create')
  def test_add_message(self, mock_create_message):
    # Mock the message creation response
    mock_message = MagicMock()
    mock_message.id = "test_message_id"
    mock_create_message.return_value = mock_message

    thread = Thread("test_thread_id", "test_assistant_id", DummyModel)
    message_id = thread.add_message("test message")

    assert message_id == "test_message_id"
    mock_create_message.assert_called_once_with(
        thread_id="test_thread_id",
        role="user",
        content="test message"
    )

  @patch('openai.beta.threads.runs.create_and_poll')
  @patch('openai.beta.threads.messages.list')
  def test_run(self, mock_list_messages, mock_create_and_poll):
    # Mock the run creation response
    mock_run = MagicMock()
    mock_run.status = "completed"
    mock_create_and_poll.return_value = mock_run

    # Mock the messages list response
    mock_list_response = MagicMock()
    mock_content = MagicMock()
    mock_content.type = 'text'
    mock_content.text = MagicMock()
    mock_content.text.value = '{"name": "test", "age": 25}'
    mock_list_response.data = [MagicMock(content=[mock_content])]
    mock_list_messages.return_value = mock_list_response

    thread = Thread("test_thread_id", "test_assistant_id", DummyModel)
    response = thread.run()

    assert response.name == "test"
    assert response.age == 25

    mock_create_and_poll.assert_called_once_with(
        thread_id="test_thread_id",
        assistant_id="test_assistant_id"
    )
    mock_list_messages.assert_called_once_with(
        thread_id="test_thread_id",
        limit=1
    )

  @patch('openai.beta.threads.messages.list')
  def test_get_last_message_json_error(self, mock_list_messages):
    # Mock the messages list response
    mock_list_response = MagicMock()
    mock_content = MagicMock()
    mock_content.type = 'text'
    mock_content.text = MagicMock()
    mock_content.text.value = 'invalid json content'
    mock_list_response.data = [MagicMock(content=[mock_content])]
    mock_list_messages.return_value = mock_list_response

    thread = Thread("test_thread_id", "test_assistant_id", DummyModel)

    with pytest.raises(ValueError, match="Invalid JSON response"):
      thread.get_last_message()

    mock_list_messages.assert_called_once_with(
        thread_id="test_thread_id",
        limit=1
    )

  @patch('openai.beta.threads.messages.list')
  def test_get_last_message(self, mock_list_messages):
    # Mock the messages list response
    mock_list_response = MagicMock()
    mock_content = MagicMock()
    mock_content.type = 'text'
    mock_content.text = MagicMock()
    mock_content.text.value = '{"name": "test", "age": 25}'
    mock_list_response.data = [MagicMock(content=[mock_content])]
    mock_list_messages.return_value = mock_list_response

    thread = Thread("test_thread_id", "test_assistant_id", DummyModel)
    response = thread.get_last_message()

    assert response.name == "test"
    assert response.age == 25
    mock_list_messages.assert_called_once_with(
        thread_id="test_thread_id",
        limit=1
    )
