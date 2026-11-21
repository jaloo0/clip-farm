# Clip Farm dashboard

## Run locally

```bash
python3 -m pip install -r dashboard-requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
streamlit run streamlit_app.py
```

Edit `.streamlit/secrets.toml` and add:

- `GITHUB_TOKEN`: a fine-grained GitHub token for this repository
- `GITHUB_REPOSITORY`: `owner/repository`
- `DASHBOARD_PASSWORD`: a private password for the dashboard

The token needs repository access to **Contents**, **Actions**, and **Releases** with write permission.

## Use

1. Upload matching files such as `clip01.mp4` and `clip01.job.json`, or a ZIP bundle containing a root-level `manifest.json` and the MP4s it lists.
2. Choose a layout or keep it on `auto`.
3. Start the batch.
4. Open the Actions run or the source Release from the success screen.

For ZIP bundles, each manifest entry needs `file`, `start`, `end`, and `title` fields; `start` and `end` use `HH:MM:SS`. The dashboard creates a render job per clip, using the clip's full duration and title as its on-screen hook. Optional `reason` and `score` fields are retained in the generated job. If `clips.md` contains a `Source: https://...` line, its YouTube source link is also included when available.

The ZIP manifest does not provide word-level transcript timings, so bundle-generated clips render without word-by-word captions and may be flagged by caption quality checks. Upload MP4/`.job.json` pairs when timed captions are needed.

The dashboard creates a tagged Release, uploads the MP4/job pairs, and dispatches the existing `render.yml` workflow.

## Local test folder

Place a test MP4 and its matching `.job.json` in `test_clips/`, then run:

```bash
python3 -m pip install -r render/requirements.txt
python3 render/render_cli.py test_clips test_clips/output --layout auto
```

Use only a small sample clip here; GitHub Actions can download it for testing, and normal dashboard batches do not use this folder.
