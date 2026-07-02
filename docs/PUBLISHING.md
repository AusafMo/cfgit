# Publishing

cfgit publishes two Python distributions:

- `cfgit`: Git-style history, diff, drift detection, branch/PR review, and
  rollback for live database records without migrating or owning the datastore.
- `cfgit-impact`: optional plugin for deterministic system-impact summaries and
  opt-in LLM narration of database record diffs.

Current release version: `0.1.2`.

## One-Time PyPI Setup

Create both projects on PyPI and configure Trusted Publishing for this repository.

For `cfgit`:

- Owner: `AusafMo`
- Repository: `cfgit`
- Workflow: `publish.yml`
- Environment: `pypi-cfgit`

For `cfgit-impact`:

- Owner: `AusafMo`
- Repository: `cfgit`
- Workflow: `publish.yml`
- Environment: `pypi-cfgit-impact`

The workflow uses PyPI OpenID Connect, so no PyPI API token is stored in GitHub
Secrets.

## Local Build Check

From the repository root:

```bash
python -m pip install -U build twine
rm -rf dist plugins/cfg_impact/dist
python -m build
python -m twine check dist/*
cd plugins/cfg_impact
python -m build
python -m twine check dist/*
```

## Clean Install Smoke

```bash
python -m venv /tmp/cfgit-publish-smoke
/tmp/cfgit-publish-smoke/bin/python -m pip install \
  'dist/cfgit-0.1.2-py3-none-any.whl[mcp]' \
  plugins/cfg_impact/dist/cfgit_impact-0.1.2-py3-none-any.whl
/tmp/cfgit-publish-smoke/bin/cfg --help
/tmp/cfgit-publish-smoke/bin/python -c 'import cfg; import cfg.mcp.server; import cfg_impact; print("imports ok")'
```

## Publish

After Trusted Publishing is configured on PyPI, publish by creating a GitHub
release from a tag:

```bash
git checkout main
git pull origin main
git tag v0.1.2
git push origin v0.1.2
gh release create v0.1.2 --title "v0.1.2" --notes "cfgit 0.1.2 package release."
```

The release triggers `.github/workflows/publish.yml`, which publishes `cfgit`
first and `cfgit-impact` second.

## Install

```bash
pip install cfgit
pip install 'cfgit[mongo]'
pip install 'cfgit[postgres]'
pip install 'cfgit[mongo,postgres,mcp]'
pip install cfgit-impact
```
