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

1. Upload matching files such as `clip01.mp4` and `clip01.job.json`.
2. Choose a layout or keep it on `auto`.
3. Start the batch.
4. Open the Actions run or the source Release from the success screen.

The dashboard creates a tagged Release, uploads the MP4/job pairs, and dispatches the existing `render.yml` workflow.
