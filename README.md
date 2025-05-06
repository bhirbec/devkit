# PyKit

A collection of Python utilities.

## Installation

- Python 3.13.3 or higher
- pip (Python package installer)

## Setting up the Virtual Environment

Create a new virtual environment:

```bash
# Using venv (built into Python 3.9+)
python3 -m venv venv
```

Activate the virtual environment:

```bash
source venv/bin/activate
```

You should see `(venv)` at the beginning of your command prompt when the virtual environment is
activated.

## Installing Requirements

Install `poetry`

```bash
pip install --upgrade pip
pip install poetry
```

Install the required packages:

```bash
poetry install --with dev
```

## Deactivating the Virtual Environment

When you're done working, you can deactivate the virtual environment:

```bash
deactivate
```

## Additional Notes

- Always activate the virtual environment before running the backend server
- Keep your requirements.txt up to date by running `pip freeze > requirements.txt` when adding new
  packages
- If you need to install additional packages, do so while the virtual environment is activated

## Testing

Make sure you have installed all dependencies as described in the [Installation](#Installation)
section.

Run tests using PyTest:

```bash
pytest
```

To test a specific file:

```bash
pytest tests/test_file.py
```

For verbose output:

```bash
pytest -v
```

To run tests with coverage:

```bash
pytest --cov=.
```
