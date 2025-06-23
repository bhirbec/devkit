import pytest
from pykit.yaml_utils import parse_file


class TestIncludeMarkdownFile:
  @pytest.fixture
  def test_files(self, tmp_path):
    # Create test files
    main = "content: !include 'content.md'"
    included = "# Test Content"

    # Write files
    (tmp_path / "main.yaml").write_text(main)
    (tmp_path / "content.md").write_text(included)

    return tmp_path

  def test_include_markdown(self, test_files):
    """Test basic YAML include of a Markdown file."""
    result = parse_file(str(test_files / "main.yaml"))
    assert result["content"] == "# Test Content"


class TestIncludeYamlFile:
  @pytest.fixture
  def test_files(self, tmp_path):
    # Create test files
    main = "content: !include 'content.yaml'"
    included = "{ test: test }"

    # Write files
    (tmp_path / "main.yaml").write_text(main)
    (tmp_path / "content.yaml").write_text(included)

    return tmp_path

  def test_include_markdown(self, test_files):
    """Test basic YAML include of a Markdown file."""
    result = parse_file(str(test_files / "main.yaml"))
    assert result["content"]["test"] == "test"


class TestNestedInclude:
  @pytest.fixture
  def test_files(self, tmp_path):
    # Create test files
    main = "content: !include 'content.md'"
    content_md = "hello !include 'nested.md'"
    nested_md = "world!"

    # Write files
    (tmp_path / "main.yaml").write_text(main)
    (tmp_path / "content.md").write_text(content_md)
    (tmp_path / "nested.md").write_text(nested_md)

    return tmp_path

  def test_nested_markdown_includes(self, test_files):
    """Test nested includes in Markdown files."""
    result = parse_file(str(test_files / "main.yaml"))
    expected_content = "hello world!"
    assert result["content"] == expected_content
