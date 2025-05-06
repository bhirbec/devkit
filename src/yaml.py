import os
import yaml


def include_constructor(loader, node):
  # Get the file path from the YAML node
  file_path = node.value
  current_dir = os.path.dirname(loader.name)  # Get the directory of the current YAML file
  full_path = os.path.join(current_dir, file_path)  # Construct the full path

  # Check file extension to determine how to read the file
  _, file_extension = os.path.splitext(full_path)
  if file_extension == '.md':
    # If it's a markdown file, read it as plain text
    with open(full_path, 'r') as file:
      return file.read()
  else:
    # If it's a YAML file, use the original `yaml_include` logic
    with open(full_path, 'r') as file:
      return yaml.safe_load(file)


yaml.add_constructor('!include', include_constructor)


def parse_file(file_path: str):
  """
  Load a YAML file and return the parsed content.
  """
  with open(file_path) as f:
    return yaml.full_load(f)
