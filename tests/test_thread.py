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

    thread = Thread("test_thread_id", "test_assistant_id")
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

    thread = Thread("test_thread_id", "test_assistant_id")
    response = thread.run(DummyModel)

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
  def test_get_last_message_without_model(self, mock_list_messages):
    # Mock the messages list response
    mock_list_response = MagicMock()
    mock_content = MagicMock()
    mock_content.type = 'text'
    mock_content.text = MagicMock()
    mock_content.text.value = 'test message content'
    mock_list_response.data = [MagicMock(content=[mock_content])]
    mock_list_messages.return_value = mock_list_response

    thread = Thread("test_thread_id", "test_assistant_id")
    content = thread.get_last_message()

    assert content == mock_content
    mock_list_messages.assert_called_once_with(
        thread_id="test_thread_id",
        limit=1
    )

  @patch('openai.beta.threads.messages.list')
  def test_get_last_message_with_model(self, mock_list_messages):
    # Mock the messages list response
    mock_list_response = MagicMock()
    mock_content = MagicMock()
    mock_content.type = 'text'
    mock_content.text = MagicMock()
    mock_content.text.value = '{"name": "test", "age": 25}'
    mock_list_response.data = [MagicMock(content=[mock_content])]
    mock_list_messages.return_value = mock_list_response

    thread = Thread("test_thread_id", "test_assistant_id")
    response = thread.get_last_message(DummyModel)

    assert response.name == "test"
    assert response.age == 25
    mock_list_messages.assert_called_once_with(
        thread_id="test_thread_id",
        limit=1
    )
