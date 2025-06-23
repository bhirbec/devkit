# PyKit

A collection of Python utilities.

## Installation

- Python 3.13.3 or higher
- pip (Python package installer)

## Setting up the Virtual Environment

Create and activate a new virtual environment using [uv](https://github.com/astral-sh/uv):

```bash
uv venv
```

## Installing Requirements

Install the required packages:

```bash
uv pip install -r pyproject.toml
```

To install dev dependencies:

```bash
uv pip install -r pyproject.toml --extra dev
```

## Testing

Make sure you have installed all dependencies as described in the [Installation](#Installation)
section.

Run tests using PyTest:

```bash
uv run pytest
```

To test a specific file:

```bash
uv run pytest tests/test_yaml.py
```

For verbose output:

```bash
uv run pytest -v
```

To run tests with coverage:

```bash
uv run pytest --cov=.
```
