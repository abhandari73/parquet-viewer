# Releasing

1. Bump `__version__` in `src/parquet_viewer/__init__.py` (e.g. `1.0.1`) and commit.
2. On GitHub: Releases → Draft a new release → tag `v1.0.1` (must match the version) → write notes → Publish.
3. The `publish` workflow tests the package and uploads it to PyPI as `parquet-browser`.

One-time setup (already needed for the first release): on pypi.org, Account → Publishing →
add a pending publisher for project `parquet-browser`, owner `abhandari73`, repository
`parquet-viewer`, workflow `publish.yml`, environment `pypi`.
