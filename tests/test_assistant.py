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
    mock_assistant.id = "test_assistant_id"
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
    mock_update.return_value = MagicMock()

    # Create a non-coroutine function to replace the async update method
    def sync_update():
      return None

    # Mock the update method and _get_remote_config
    with patch('pykit.assistant.Assistant._get_remote_config', return_value=mock_remote):
      with patch.object(Assistant, 'update', new=sync_update):
        assistant = Assistant(config_path="dummy_path", model=model, env_var=env_var)

        # We need to patch the update method again to track calls
        with patch.object(assistant, 'update') as mock_update_method:
          assistant.update_if_config_has_changed()

          # Verify update was called
          mock_update_method.assert_called_once()

  @patch('openai.beta.assistants.update')
  def test_no_update_when_config_unchanged(self, mock_update, config, model, env_var, mock_parse_file):
    # Set environment variable
    os.environ[env_var] = "test_assistant_id"

    assistant = Assistant(config_path="dummy_path", model=model, env_var=env_var)
    fingerprint = assistant._compute_fingerprint(assistant.config)

    # Mock the remote assistant with same fingerprint
    mock_remote = MagicMock()
    mock_remote.metadata = {"fingerprint": fingerprint}

    # Create a non-coroutine function to replace the async update method
    def sync_update():
      return None

    # Mock the update method and _get_remote_config
    with patch('pykit.assistant.Assistant._get_remote_config', return_value=mock_remote):
      with patch.object(Assistant, 'update', new=sync_update):
        assistant = Assistant(config_path="dummy_path", model=model, env_var=env_var)

        # We need to patch the update method again to track calls
        with patch.object(assistant, 'update') as mock_update_method:
          assistant.update_if_config_has_changed()

          # Verify update was not called
          mock_update_method.assert_not_called()


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


class TestAgentInitThread:

  def test_create_thread(self, config, model, env_var, mock_parse_file):
    # Set environment variable
    os.environ[env_var] = "test_assistant_id"

    # Create the assistant and call create_thread
    assistant = Assistant(config_path="dummy_path", model=model, env_var=env_var)
    thread = assistant.init_thread('test_thread_id')

    # Verify the results
    assert thread.thread_id == "test_thread_id"
    assert thread.assistant_id == "test_assistant_id"


class TestAgentCreateThread:

  def test_create_thread(self, config, model, env_var, mock_parse_file):
    # Set environment variable
    os.environ[env_var] = "test_assistant_id"

    # Create a mock for openai.beta.threads.create
    with patch('openai.beta.threads.create') as mock_create_thread:
      # Mock the thread creation response
      mock_thread = MagicMock()
      mock_thread.id = "test_thread_id"
      mock_create_thread.return_value = mock_thread

      # Create the assistant and call create_thread
      assistant = Assistant(config_path="dummy_path", model=model, env_var=env_var)
      thread = assistant.create_thread()

      # Verify the results
      assert thread.thread_id == "test_thread_id"
      assert thread.assistant_id == "test_assistant_id"
      mock_create_thread.assert_called_once()


class TestAgentQuery:

  def test_query(self, config, model, env_var, mock_parse_file):
    # Set environment variable
    os.environ[env_var] = "test_assistant_id"

    # Create a mock thread instance
    mock_thread_instance = MagicMock()
    mock_thread_instance.add_message = MagicMock(return_value="test_message_id")
    mock_thread_instance.run = MagicMock(return_value=DummyModel(name="test", age=25))

    # Create the assistant
    assistant = Assistant(config_path="dummy_path", model=model, env_var=env_var)

    # Mock create_thread to return the mock thread instance directly (not as a coroutine)
    with patch.object(assistant, 'create_thread', return_value=mock_thread_instance):
      # Execute the query
      response = assistant.query("test query")

      # Verify the results
      assert response.name == "test"
      assert response.age == 25

      # Verify the thread methods were called correctly
      mock_thread_instance.add_message.assert_called_once_with("test query")
      mock_thread_instance.run.assert_called_once()
