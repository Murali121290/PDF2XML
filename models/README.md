# Pre-downloaded Docling models

The API defaults to Docling and uses these local models without runtime downloads.

After installing `.[ml]`, prepare them once with internet access:

```bash
python tools/prepare_models.py
```

Or copy an existing complete model folder without using the internet:

```bash
python tools/prepare_models.py --source /path/to/existing/models
```

`manifest.json` pins the model revisions and SHA-256 checksums. Preparation verifies every
required file. `python tools/prepare_models.py --check` only checks local files; it never
downloads. The weights (about 530 MB) are excluded from Git.

Docker copies this folder and verifies it during the build. If the weights are absent in a
fresh clone, it downloads the pinned files at build time. The finished image contains all
required models and can convert without internet access. Keep the model repositories'
README files with the weights; they include model information and license declarations.
