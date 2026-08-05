# Contributing to Hiero Maintainer Bot

Thank you for your interest in contributing to **Hiero Maintainer Bot**! Contributions of all sizes are welcome, including bug fixes, new features, documentation improvements, and test enhancements.

## Before You Start

- Search the existing issues before creating a new one.
- For larger features or breaking changes, open an issue first to discuss the proposed implementation.
- Keep pull requests focused on a single feature or fix whenever possible.

## Development Setup

Follow the instructions in [SETUP.md](SETUP.md) to configure the project locally.

## Create a Branch

Create a new branch from the latest `main` branch.

```bash
git checkout main
git pull origin main
git checkout -b feature/my-feature
```

Use descriptive branch names, for example:

- `feature/reviewer-improvements`
- `fix/pr-health-scoring`
- `docs/setup-guide`
- `test/add-api-tests`

## Make Your Changes

Please follow the existing project style and conventions.

When adding new functionality:

- Keep functions focused and easy to understand.
- Add or update tests when applicable.
- Update documentation if user-facing behavior changes.
- Avoid unrelated formatting or refactoring changes in the same pull request.

## Run the Development Checks

Before opening a pull request, ensure all checks pass.

### Run the test suite

```bash
pytest
```

Or with coverage:

```bash
python -m pytest tests/ --cov=app --cov-report=term-missing
```

### Run Ruff

```bash
ruff check .
```

Automatically fix issues where possible:

```bash
ruff check . --fix
```

### Run the Security Audit

```bash
pip-audit -r requirements.txt -f json -o audit-report.json
python .github/scripts/check_audit_severity.py audit-report.json
```

## Commit Your Changes

Write clear and descriptive commit messages.

Examples:

```text
feat: add reviewer recommendation caching
fix: prevent duplicate onboarding comments
docs: add local setup guide
test: improve PR health workflow coverage
```

If your changes modify workflow logic, include corresponding tests whenever possible.

## Open a Pull Request

When submitting a pull request:

- Clearly describe the purpose of the change.
- Reference any related issues using keywords such as `Fixes #123`.
- Include screenshots for dashboard or UI changes when appropriate.
- Keep pull requests small and focused.

## Pull Request Checklist

Before requesting a review, verify that:

- [ ] The project builds successfully.
- [ ] All tests pass.
- [ ] Ruff reports no lint issues.
- [ ] Documentation has been updated if needed.
- [ ] New functionality includes appropriate tests.
- [ ] The pull request addresses a single feature or bug.

## Code Style

Please follow the existing coding style used throughout the project.

- Use meaningful variable and function names.
- Prefer small, focused functions.
- Keep modules organized by responsibility.
- Follow existing FastAPI and SQLAlchemy patterns.

## Reporting Bugs

When reporting a bug, include:

- Operating system
- Python version
- Steps to reproduce
- Expected behavior
- Actual behavior
- Relevant logs or screenshots

## Feature Requests

Feature requests are welcome.

Please describe:

- The problem you are trying to solve.
- Your proposed solution.
- Any alternatives you considered.

## Questions

If you have questions about contributing, feel free to open a discussion or create an issue.

Thank you for helping improve Hiero Maintainer Bot!