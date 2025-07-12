from pykit.templating.md import render, process_includes


class TestProcessIncludes:
  def test_process_includes_basic(self, tmp_path):
    """Test basic include processing in a string."""
    # Create test files
    main_content = "Hello !include 'included.md'"
    included_content = "World!"

    main_path = tmp_path / "main.md"
    included_path = tmp_path / "included.md"

    main_path.write_text(main_content)
    included_path.write_text(included_content)

    result = process_includes(main_content, str(main_path))
    assert result == "Hello World!"

  def test_process_includes_nested(self, tmp_path):
    """Test nested include processing in a string."""
    # Create test files
    main_content = "Hello !include 'level1.md'"
    level1_content = "Level 1 !include 'level2.md'"
    level2_content = "Level 2!"

    main_path = tmp_path / "main.md"
    level1_path = tmp_path / "level1.md"
    level2_path = tmp_path / "level2.md"

    main_path.write_text(main_content)
    level1_path.write_text(level1_content)
    level2_path.write_text(level2_content)

    result = process_includes(main_content, str(main_path))
    assert result == "Hello Level 1 Level 2!"

  def test_process_includes_missing_file(self, tmp_path):
    """Test handling of missing included files."""
    main_content = "Hello !include 'missing.md'"
    main_path = tmp_path / "main.md"
    main_path.write_text(main_content)

    result = process_includes(main_content, str(main_path))
    assert result == "Hello [Error: Could not include file 'missing.md']"


class TestParseFile:
  def test_render_basic(self, tmp_path):
    """Test basic file parsing with includes."""
    # Create test files
    main_content = "Hello !include 'included.md'"
    included_content = "World!"

    main_path = tmp_path / "main.md"
    included_path = tmp_path / "included.md"

    main_path.write_text(main_content)
    included_path.write_text(included_content)

    result = render(str(main_path))
    assert result == "Hello World!"

  def test_render_nested(self, tmp_path):
    """Test file parsing with nested includes."""
    # Create test files
    main_content = "Start !include 'level1.md' End"
    level1_content = "Level 1 !include 'level2.md' Middle"
    level2_content = "Level 2!"

    main_path = tmp_path / "main.md"
    level1_path = tmp_path / "level1.md"
    level2_path = tmp_path / "level2.md"

    main_path.write_text(main_content)
    level1_path.write_text(level1_content)
    level2_path.write_text(level2_content)

    result = render(str(main_path))
    assert result == "Start Level 1 Level 2! Middle End"
