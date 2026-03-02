# Contributing to Turnstone

Thanks for your interest in contributing! Here's what you need to know.

## Contributor License Agreement

All contributors must agree to the [Contributor License Agreement](CLA.md)
before their pull request can be merged. When you open your first PR, the CLA
Assistant bot will ask you to sign by commenting on the PR. This is a one-time
process.

The CLA allows us to distribute Turnstone under both open-source and commercial
licenses. Your contributions remain your own — you are granting a license, not
transferring ownership.

## Getting Started

1. Fork the repository
2. Create a branch for your change
3. Make your changes
4. Run the tests: `pytest`
5. Open a pull request

## Development Setup

```
python -m venv .venv
source .venv/bin/activate
pip install -e ".[test]"
```

## Guidelines

- Keep pull requests focused — one change per PR
- Add tests for new functionality
- Follow existing code style and patterns
- Update documentation if your change affects user-facing behavior

## Reporting Issues

Open an issue at https://github.com/turnstonelabs/turnstone/issues with:

- What you expected to happen
- What actually happened
- Steps to reproduce
- Python version and OS

## License

By contributing, you agree that your contributions will be licensed under the
project's [Business Source License 1.1](LICENSE).
