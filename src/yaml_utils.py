import os
import yaml
import re


def process_markdown_includes(content: str, base_path: str) -> str:
  """Process !include statements in Markdown content."""
  def replace_include(match):
    include_path = match.group(1)
    full_path = os.path.join(os.path.dirname(base_path), include_path)

    try:
      with open(full_path, 'r') as f:
        included_content = f.read()
        return process_markdown_includes(included_content, full_path)
    except FileNotFoundError:
      return f"[Error: Could not include file '{include_path}']"

  pattern = r'!include\s+[\'"](.+?)[\'"]'
  return re.sub(pattern, replace_include, content)


def include_constructor(loader, node):
  file_path = node.value
  current_dir = os.path.dirname(loader.name)
  full_path = os.path.join(current_dir, file_path)

  _, file_extension = os.path.splitext(full_path)
  with open(full_path, 'r') as file:
    content = file.read()

    if file_extension == '.md':
      return process_markdown_includes(content, full_path)
    else:
      return yaml.safe_load(content)


yaml.add_constructor('!include', include_constructor)


def parse_file(file_path: str):
  """Load a YAML file and return the parsed content."""
  with open(file_path) as f:
    return yaml.full_load(f)
