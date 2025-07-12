import logging
import os
import tempfile
from typing import Dict, Any

import openai

# logging
logger = logging.getLogger(__name__)


class VectorStore:
  """
  Represents an OpenAI vector store for file embeddings.
  """

  def __init__(self, vector_store_id: str):
    """
    Initialize a vector store with its ID.

    Args:
      vector_store_id: The ID of the vector store
    """
    self.vector_store_id = vector_store_id

  def create(self, name: str) -> Any:
    """
    Create a new vector store.

    Args:
      name: The name of the vector store

    Returns:
      The created vector store object
    """
    vector_store = openai.vector_stores.create(name=name)
    self.vector_store_id = vector_store.id
    logger.info(f"Vector store successfully created: {self.vector_store_id}")
    return vector_store

  def add_file(self, content: str) -> Any:
    """
    Upload a file and attach it to the vector store.

    Args:
      content: The content of the file to upload

    Returns:
      The vector store file object
    """
    # Create a temporary file
    temp = tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl")
    file_path = temp.name

    # Write content to the temporary file
    with open(file_path, 'w') as f:
      f.write(content)

    try:
      # Upload the file
      with open(file_path, 'rb') as f:
        file = openai.files.create(
            file=f,
            purpose="assistants"
        )
        logger.info(f"Uploaded file with ID: {file.id}")

      # Attach the file to the vector store
      vector_store_file = openai.vector_stores.files.create(
          vector_store_id=self.vector_store_id,
          file_id=file.id
      )
      logger.info(f"File {file.id} successfully attached to vector store {self.vector_store_id}")

      return vector_store_file
    finally:
      # Clean up the temporary file
      if os.path.exists(file_path):
        os.remove(file_path)
