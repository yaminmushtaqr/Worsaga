# Release Process

Worsaga releases are published to PyPI.

## Pre-release checks

Run from a clean checkout:

```bash
git status --short
bash scripts/audit_public_release.sh
python -m pip install --upgrade build twine
pytest
python -m build
python -m twine check dist/*
```

Inspect package contents:

```bash
tar -tzf dist/worsaga-*.tar.gz | sort
python -m zipfile --list dist/worsaga-*.whl
```

Confirm the source distribution and wheel do not include credentials, local
planning files, private deployment notes, or institution-specific data. The
audit script also unpacks and scans both built artifacts.

## Repository hygiene

If a private repository previously contained personal planning or private
release history, do not simply flip that repository public. Publish from a
fresh public repository or a clean public branch with only release-safe
history.

Ignoring a file prevents future commits, but it does not remove old content
from git history.

## Versioning

1. Update `pyproject.toml`.
2. Update `src/worsaga/__init__.py`.
3. Update `CHANGELOG.md`.
4. Commit the release.
5. Tag with `vX.Y.Z` only after all checks pass.

## Publish via trusted publishing

The `publish.yml` workflow publishes tagged releases to PyPI using trusted
publishing. Configure the PyPI project to trust the GitHub repository,
environment, and workflow before running it.

To publish:

```bash
git tag vX.Y.Z
git push origin vX.Y.Z
```

The workflow audits, tests, builds, checks, and publishes the package.

## Manual fallback

Use a scoped PyPI API token only if trusted publishing is unavailable:

```bash
python -m twine upload dist/*
```

Never commit PyPI tokens.
