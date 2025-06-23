import os
import pytest

from pydantic import BaseModel
from unittest.mock import patch, MagicMock

from pykit.assistant import Assistant


@pytest.fixture(autouse=True)
def setup_env():
  os.environ["OPENAI_API_KEY"] = "dummy-key"


@pytest.fixture
def mock_parse_file(config):
  with patch('pykit.assistant.parse_file', return_value=config) as mock:
    yield mock


@pytest.fixture
def config():
  return {
      "name": "Test Assistant",
      "instructions": "A test assistant",
      "model": "gpt-4-turbo-preview",
      "tools": []
  }


@pytest.fixture
def model():
  return DummyModel


@pytest.fixture
def env_var():
  return "TEST_ASSISTANT_ID"


class DummyModel(BaseModel):
  name: str
  age: int


class TestAgentInit:
  def test_init(self, config, model, env_var, mock_parse_file):
    assistant = Assistant(config_path="dummy_path", model=model, env_var=env_var)
    assert assistant.env_var == env_var
    assert assistant.config == config


class TestAgentCreate:
  @patch('openai.beta.assistants.create')
  @patch('pykit.assistant.set_key')
  def test_create(self, mock_set_key, mock_create, config, model, env_var, mock_parse_file):
    # Mock the assistant creation response
    mock_assistant = MagicMock()
    mock_assistant.id = "test_assistant_id"
    mock_create.return_value = mock_assistant

    assistant = Assistant(config_path="dummy_path", model=model, env_var=env_var)
    assistant.create()

    # Verify OpenAI API was called with correct config
    mock_create.assert_called_once()
    mock_set_key.assert_called_once_with('.env', env_var, mock_assistant.id)


class TestAgentUpdate:

  @patch('openai.beta.assistants.update')
  def test_update(self, mock_update, config, model, env_var, mock_parse_file):
    # Set environment variable
    os.environ[env_var] = "test_assistant_id"

    # Mock the assistant update response
    mock_assistant = MagicMock()
    mock_update.return_value = mock_assistant

    assistant = Assistant(config_path="dummy_path", model=model, env_var=env_var)
    assistant.update()

    # Verify OpenAI API was called with correct config
    mock_update.assert_called_once()


class TestAgentUpdateIfConfigChanged:
  @patch('openai.beta.assistants.update')
  def test_update_when_config_changed(self, mock_update, config, model, env_var, mock_parse_file):
    # Set environment variable
    os.environ[env_var] = "test_assistant_id"

    # Mock the remote assistant with different fingerprint
    mock_remote = MagicMock()
    mock_remote.metadata = {"fingerprint": "different_fingerprint"}

    with patch('pykit.assistant.Assistant._get_remote_config', return_value=mock_remote):
      assistant = Assistant(config_path="dummy_path", model=model, env_var=env_var)
      assistant.update_if_config_has_changed()

    # Verify update was called
    mock_update.assert_called_once()

  @patch('openai.beta.assistants.update')
  def test_no_update_when_config_unchanged(self, mock_update, config, model, env_var, mock_parse_file):
    # Set environment variable
    os.environ[env_var] = "test_assistant_id"

    assistant = Assistant(config_path="dummy_path", model=model, env_var=env_var)
    fingerprint = assistant._compute_fingerprint(assistant.config)

    # Mock the remote assistant with same fingerprint
    mock_remote = MagicMock()
    mock_remote.metadata = {"fingerprint": fingerprint}

    with patch('pykit.assistant.Assistant._get_remote_config', return_value=mock_remote):
      assistant.update_if_config_has_changed()

    # Verify update was not called
    mock_update.assert_not_called()


class TestAgentDelete:
  @patch('openai.beta.assistants.delete')
  def test_delete(self, mock_delete, config, model, env_var, mock_parse_file):
    # Set environment variable
    os.environ[env_var] = "test_assistant_id"

    # Mock the assistant deletion response
    mock_assistant = MagicMock()
    mock_delete.return_value = mock_assistant

    assistant = Assistant(config_path="dummy_path", model=model, env_var=env_var)
    assistant.delete()

    # Verify OpenAI API was called with correct assistant ID
    mock_delete.assert_called_once_with("test_assistant_id")


class TestAgentQuery:
  @patch('openai.beta.threads.messages.create')
  @patch('openai.beta.threads.runs.create_and_poll')
  @patch('openai.beta.threads.messages.list')
  @patch('openai.beta.threads.create')
  def test_query(self, mock_create_thread, mock_list_messages, mock_create_and_poll, mock_create_message, config, model, env_var, mock_parse_file):
    # Set environment variable
    os.environ[env_var] = "test_assistant_id"

    # Mock the thread creation response
    mock_thread = MagicMock()
    mock_thread.id = "test_thread_id"
    mock_create_thread.return_value = mock_thread

    # Mock the message creation response
    mock_message = MagicMock()
    mock_message.id = "test_message_id"
    mock_create_message.return_value = mock_message

    # Mock the run creation and polling response
    mock_run = MagicMock()
    mock_run.id = "test_run_id"
    mock_run.status = "completed"
    mock_create_and_poll.return_value = mock_run

    # Mock the messages list response
    mock_list_response = MagicMock()
    mock_content = MagicMock()
    mock_content.text = MagicMock()
    mock_content.text.value = '{"name": "test", "age": 25}'
    mock_list_response.data = [MagicMock(content=[mock_content])]
    mock_list_messages.return_value = mock_list_response

    assistant = Assistant(config_path="dummy_path", model=model, env_var=env_var)
    response = assistant.query("test query")
    assert response.name == "test"
    assert response.age == 25
