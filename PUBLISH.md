# QOREgeo — Publish Guide

Step-by-step instructions to publish QOREgeo to GitHub and PyPI.

---

## Step 1 — Create the GitHub Repository

1. Go to https://github.com/bosekarmegam
2. Click **New repository**
3. Name it: `qoregeo`
4. Description: `Quantum-Powered Spatial Intelligence for Python. Zero dependencies.`
5. Set to **Public**
6. Do NOT add README (we already have one)
7. Click **Create repository**

---

## Step 2 — Push to GitHub

Run these commands in the `qoregeo/` folder:

```bash
cd qoregeo/

# Initialize git
git init
git add .
git commit -m "feat: initial release v1.0.0"

# Add your remote
git remote add origin https://github.com/bosekarmegam/qoregeo.git

# Push
git branch -M main
git push -u origin main
```

---

## Step 3 — Create PyPI Account

1. Go to https://pypi.org/account/register/
2. Register with your email (suneel@arcgx.in)
3. Verify your email
4. Enable 2FA (required for publishing)

---

## Step 4 — Build the Package

```bash
# Install build tools
pip install build hatchling

# Build wheel + source distribution
python -m build

# You should see:
#   dist/qoregeo-1.0.0.tar.gz
#   dist/qoregeo-1.0.0-py3-none-any.whl
```

---

## Step 5 — Test on TestPyPI First (Recommended)

```bash
# Install twine
pip install twine

# Upload to TestPyPI
python -m twine upload --repository testpypi dist/*

# Test the install
pip install --index-url https://test.pypi.org/simple/ qoregeo
```

---

## Step 6 — Publish to Real PyPI

```bash
# Upload to PyPI
python -m twine upload dist/*

# Enter your PyPI username and password (or API token)
```

After this, anyone can run:
```bash
pip install qoregeo
```

---

## Step 7 — Enable GitHub Actions (Auto-Publish)

The `.github/workflows/publish.yml` file will automatically publish to PyPI
whenever you create a GitHub Release.

To set it up:

1. Go to your PyPI account → API Tokens → Add API token
2. Go to GitHub repo → Settings → Secrets → Actions
3. Add secret: `PYPI_API_TOKEN` = (paste token)
4. Update `publish.yml` to use the token:

```yaml
- name: Publish to PyPI
  uses: pypa/gh-action-pypi-publish@release/v1
  with:
    password: ${{ secrets.PYPI_API_TOKEN }}
```

Then to release:
```bash
git tag v1.0.0
git push origin v1.0.0
# Create a GitHub Release from this tag
```

---

## Step 8 — After Publishing

Verify at: https://pypi.org/project/qoregeo

Test fresh install:
```bash
pip install qoregeo
python -c "from qoregeo import GeoEngine; print(GeoEngine.VERSION)"
# 1.0.0
```

---

## Running Tests Locally

```bash
pip install pytest pytest-cov
pytest tests/ -v
pytest tests/ --cov=qoregeo --cov-report=term-missing
```

---

## Release Checklist

Before every release:

- [ ] All tests pass (`pytest tests/ -v`)
- [ ] Version bumped in `pyproject.toml` AND `qoregeo/__init__.py`
- [ ] CHANGELOG.md updated
- [ ] README.md up to date
- [ ] Git tag created (`git tag vX.X.X`)
- [ ] GitHub Release created
- [ ] PyPI auto-publish triggered via GitHub Actions
