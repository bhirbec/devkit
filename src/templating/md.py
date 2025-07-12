import os
import re


def process_includes(content: str, base_path: str) -> str:
  """Process !include statements in Markdown content."""
  def replace_include(match):
    include_path = match.group(1)
    full_path = os.path.join(os.path.dirname(base_path), include_path)

    try:
      with open(full_path, 'r') as f:
        included_content = f.read()
        return process_includes(included_content, full_path)
    except FileNotFoundError:
      return f"[Error: Could not include file '{include_path}']"

  pattern = r'!include\s+[\'"](.+?)[\'"]'
  return re.sub(pattern, replace_include, content)


def render(file_path: str) -> str:
  """Load a Markdown file and return the processed content with all includes resolved."""
  with open(file_path, 'r') as f:
    content = f.read()
    return process_includes(content, file_path)
