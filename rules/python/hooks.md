---
paths:
  - "**/*.py"
  - "**/*.pyi"
---
# Python Hooks


## PostToolUse Hooks

If these tools are already installed, consider configuring optional hooks:

- **black/ruff**: Auto-format `.py` files after edit
- **mypy/pyright**: Run type checking after editing `.py` files

## Warnings

- Warn about `print()` statements in edited files (use `logging` module instead)
