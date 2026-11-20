from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from io import BytesIO

import requests
import streamlit as st


API = "https://api.github.com"


def secret(name: str, default: str = "") -> str:
    try:
        value = st.secrets[name]
    except (KeyError, FileNotFoundError):
        value = default
    return str(value).strip()


GITHUB_TOKEN = secret("GITHUB_TOKEN")
GITHUB_REPOSITORY = secret("GITHUB_REPOSITORY", "jaloo0/clip-farm")
DASHBOARD_PASSWORD = secret("DASHBOARD_PASSWORD")


def gh(method: str, path: str, **kwargs) -> requests.Response:
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    response = requests.request(method, f"{API}{path}", headers=headers, timeout=120, **kwargs)
    if not response.ok:
        detail = response.text[:1000]
        raise RuntimeError(f"GitHub API {response.status_code}: {detail}")
    return response


def find_release(tag: str) -> dict | None:
    response = gh("GET", f"/repos/{GITHUB_REPOSITORY}/releases/tags/{tag}")
    return response.json()


def create_release(tag: str) -> dict:
    response = gh(
        "POST",
        f"/repos/{GITHUB_REPOSITORY}/releases",
        json={
            "tag_name": tag,
            "name": f"Clip batch {tag}",
            "body": "Source MP4 and job files uploaded by the Clip Farm dashboard.",
            "draft": False,
            "prerelease": False,
        },
    )
    return response.json()


def upload_asset(release_id: int, name: str, data: bytes) -> dict:
    response = requests.post(
        f"{API}/repos/{GITHUB_REPOSITORY}/releases/{release_id}/assets",
        params={"name": name},
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/octet-stream",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        data=BytesIO(data),
        timeout=600,
    )
    if not response.ok:
        raise RuntimeError(f"Upload {name} failed ({response.status_code}): {response.text[:1000]}")
    return response.json()


def pair_files(files: list) -> tuple[list[tuple[str, bytes]], list[str], list[str]]:
    mp4 = {}
    jobs = {}
    for uploaded in files:
        name = uploaded.name
        if name.lower().endswith(".mp4"):
            mp4[name[:-4]] = uploaded.getvalue()
        elif name.lower().endswith(".job.json"):
            jobs[name[:-10]] = uploaded.getvalue()

    ids = sorted(set(mp4) & set(jobs))
    errors = []
    sources = []
    for base in sorted(set(mp4) | set(jobs)):
        if base not in mp4:
            errors.append(f"Missing MP4 for {base}.job.json")
        if base not in jobs:
            errors.append(f"Missing .job.json for {base}.mp4")

    assets = []
    for base in ids:
        job_text = jobs[base]
        try:
            job = json.loads(job_text.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            errors.append(f"{base}.job.json is not valid JSON: {exc}")
            continue
        if not isinstance(job, dict):
            errors.append(f"{base}.job.json must contain a JSON object")
            continue
        if not job.get("id"):
            errors.append(f"{base}.job.json is missing \"id\"")
            continue
        job["video"] = f"{base}.mp4"
        if job.get("name") is None:
            job["name"] = base
        assets.append((f"{base}.mp4", mp4[base]))
        assets.append((f"{base}.job.json", json.dumps(job, indent=2).encode("utf-8") + b"\n"))
        sources.append(base)
    return assets, sources, errors


def dispatch_workflow(tag: str, layout: str, only: str) -> str:
    response = gh(
        "POST",
        f"/repos/{GITHUB_REPOSITORY}/actions/workflows/render.yml/dispatches",
        json={
            "ref": "main",
            "inputs": {"tag": tag, "layout": layout, "only": only},
        },
    )
    return f"https://github.com/{GITHUB_REPOSITORY}/actions/workflows/render.yml"


def authenticated() -> bool:
    if not GITHUB_TOKEN:
        st.error("Missing GITHUB_TOKEN in Streamlit secrets.")
        return False
    if not GITHUB_REPOSITORY:
        st.error("Missing GITHUB_REPOSITORY in Streamlit secrets.")
        return False
    if DASHBOARD_PASSWORD:
        if "dashboard_password_ok" not in st.session_state:
            st.session_state.dashboard_password_ok = False
        if not st.session_state.dashboard_password_ok:
            password = st.text_input("Dashboard password", type="password")
            if st.button("Sign in"):
                if password == DASHBOARD_PASSWORD:
                    st.session_state.dashboard_password_ok = True
                    st.rerun()
                else:
                    st.error("Incorrect password")
            st.stop()
    return True


st.set_page_config(page_title="Clip Farm", page_icon="🎬", layout="wide")
st.title("Clip Farm")
st.caption("Upload source clips and start vertical rendering in GitHub Actions.")

if not authenticated():
    st.stop()

layout = st.segmented_control(
    "Layout",
    options=["auto", "stack", "fullcam", "gameplay"],
    default="auto",
)
only = st.text_input("Only clip IDs", placeholder="c01,c03", help="Leave blank to render every clip.")
files = st.file_uploader(
    "Clip files",
    type=["mp4", "json"],
    accept_multiple_files=True,
    help="Upload matching pairs such as interview.mp4 and interview.job.json.",
)

if files:
    assets, sources, errors = pair_files(files)
    if sources:
        st.write("Ready to upload:", ", ".join(sources))
    for error in errors:
        st.error(error)

    if st.button("Create batch and start rendering", type="primary", disabled=bool(errors) or not assets):
        tag = "clips-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        try:
            with st.status("Uploading clips and starting GitHub Actions...", expanded=True) as status:
                st.write(f"Creating Release `{tag}`...")
                try:
                    release = find_release(tag)
                except RuntimeError:
                    release = create_release(tag)

                st.write(f"Uploading {len(assets)} files...")
                for name, data in assets:
                    upload_asset(release["id"], name, data)
                    st.write(f"Uploaded `{name}`")

                st.write("Starting workflow...")
                workflow_url = dispatch_workflow(tag, layout, only.strip())
                status.update(label="Batch started", state="complete")

            st.success("The batch is running.")
            st.link_button("Open Actions", workflow_url)
            st.link_button("Open source Release", release["html_url"])
            st.code(tag)
        except Exception as exc:
            st.error(str(exc))
else:
    st.info("Add matching `.mp4` and `.job.json` files to begin.")
