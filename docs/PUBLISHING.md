# Publishing

cfgit publishes two Python packages:

- `cfg-vcs`: core library, CLI, adapters, UI, and MCP server entry point.
- `cfg-impact`: optional system-impact / LLM narration plugin.

Current first release version: `0.1.0`.

## One-Time PyPI Setup

Create both projects on PyPI and configure Trusted Publishing for this repository.

For `cfg-vcs`:

- Owner: `AusafMo`
- Repository: `cfgit`
- Workflow: `publish.yml`
- Environment: leave blank

For `cfg-impact`, use the same trusted publisher settings.

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
  'dist/cfg_vcs-0.1.0-py3-none-any.whl[mcp]' \
  plugins/cfg_impact/dist/cfg_impact-0.1.0-py3-none-any.whl
/tmp/cfgit-publish-smoke/bin/cfg --help
/tmp/cfgit-publish-smoke/bin/python -c 'import cfg; import cfg.mcp.server; import cfg_impact; print("imports ok")'
```

## Publish

After Trusted Publishing is configured on PyPI, publish by creating a GitHub
release from a tag:

```bash
git checkout main
git pull origin main
git tag v0.1.0
git push origin v0.1.0
gh release create v0.1.0 --title "v0.1.0" --notes "First public cfgit release."
```

The release triggers `.github/workflows/publish.yml`, which publishes `cfg-vcs`
first and `cfg-impact` second.

## Install

```bash
pip install cfg-vcs
pip install 'cfg-vcs[mongo]'
pip install 'cfg-vcs[postgres]'
pip install 'cfg-vcs[mongo,postgres,mcp]'
pip install cfg-impact
```
