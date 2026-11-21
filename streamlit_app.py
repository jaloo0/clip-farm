from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from io import BytesIO
from pathlib import PurePosixPath
from urllib.parse import parse_qs, urlparse
from zipfile import BadZipFile, ZipFile

import requests
import streamlit as st


API = "https://api.github.com"
MAX_BUNDLE_ENTRIES = 256
MAX_BUNDLE_SIZE = 500 * 1024 * 1024


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


def parse_timestamp(value: object) -> float:
    if not isinstance(value, str):
        raise ValueError("timestamp must use HH:MM:SS format")
    match = re.fullmatch(r"(\d+):([0-5]\d):([0-5]\d(?:\.\d+)?)", value.strip())
    if not match:
        raise ValueError(f"invalid timestamp {value!r}; expected HH:MM:SS")
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def parse_clip_bundle(data: bytes) -> tuple[dict[str, bytes], dict[str, bytes], list[str]]:
    videos: dict[str, bytes] = {}
    jobs: dict[str, bytes] = {}
    errors = []
    try:
        archive = ZipFile(BytesIO(data))
    except BadZipFile as exc:
        return videos, jobs, [f"ZIP bundle is invalid: {exc}"]

    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_BUNDLE_ENTRIES:
            return videos, jobs, [f"ZIP bundle has too many entries (maximum {MAX_BUNDLE_ENTRIES})"]
        if sum(info.file_size for info in infos) > MAX_BUNDLE_SIZE:
            return videos, jobs, ["ZIP bundle is too large (maximum uncompressed size is 500 MB)"]

        by_name = {}
        for info in infos:
            if info.is_dir():
                continue
            if info.filename in by_name:
                errors.append(f"ZIP bundle contains duplicate entry {info.filename!r}")
            by_name[info.filename] = info

        manifest_info = by_name.get("manifest.json")
        if manifest_info is None:
            return videos, jobs, errors + ["ZIP bundle must contain a root-level manifest.json"]
        try:
            manifest = json.loads(archive.read(manifest_info).decode("utf-8-sig"))
        except (BadZipFile, RuntimeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return videos, jobs, errors + [f"manifest.json could not be read: {exc}"]
        if not isinstance(manifest, list):
            return videos, jobs, errors + ["manifest.json must contain a list of clips"]

        source_url = ""
        notes_info = by_name.get("clips.md")
        if notes_info and notes_info.file_size <= 1024 * 1024:
            try:
                notes = archive.read(notes_info).decode("utf-8-sig")
                source_match = re.search(r"^Source:\s*(https?://\S+)", notes, re.MULTILINE)
                if source_match:
                    source_url = source_match.group(1)
            except (BadZipFile, RuntimeError, UnicodeDecodeError) as exc:
                errors.append(f"clips.md could not be read: {exc}")

        for index, clip in enumerate(manifest, start=1):
            label = f"manifest clip {index}"
            if not isinstance(clip, dict):
                errors.append(f"{label} must be a JSON object")
                continue
            filename = clip.get("file")
            if (not isinstance(filename, str) or PurePosixPath(filename).name != filename
                    or not filename.lower().endswith(".mp4")):
                errors.append(f"{label} must name a root-level MP4 file")
                continue
            info = by_name.get(filename)
            if info is None:
                errors.append(f"{label} references missing {filename}")
                continue
            if filename in videos:
                errors.append(f"manifest.json references {filename} more than once")
                continue
            title = clip.get("title")
            if not isinstance(title, str) or not title.strip():
                errors.append(f"{label} is missing a non-empty title")
                continue
            try:
                start = parse_timestamp(clip.get("start"))
                end = parse_timestamp(clip.get("end"))
            except ValueError as exc:
                errors.append(f"{label}: {exc}")
                continue
            if end <= start:
                errors.append(f"{label} end must be later than start")
                continue
            try:
                video_data = archive.read(info)
            except (BadZipFile, RuntimeError) as exc:
                errors.append(f"{filename} could not be read: {exc}")
                continue

            clip_id = f"c{index:02d}"
            job = {
                "id": clip_id,
                "name": PurePosixPath(filename).stem,
                "video": filename,
                "keep": [[0, round(end - start, 3)]],
                "words": [],
                "hook": title.strip(),
                "clip_title": title.strip(),
                "clip_reason": str(clip.get("reason") or ""),
                "source_t0": start,
            }
            if "score" in clip:
                job["score"] = clip["score"]
            if source_url:
                job["source_url"] = source_url
                parsed_url = urlparse(source_url)
                host = (parsed_url.hostname or "").lower()
                video_id = parse_qs(parsed_url.query).get("v", [""])[0]
                if host == "youtu.be":
                    video_id = parsed_url.path.strip("/").split("/", 1)[0]
                if (host == "youtube.com" or host.endswith(".youtube.com") or host == "youtu.be") and video_id:
                    job["platform"] = "youtube"
                    job["stream_key"] = f"youtube-{video_id}"

            videos[filename] = video_data
            jobs[f"{PurePosixPath(filename).stem}.job.json"] = (
                json.dumps(job, indent=2).encode("utf-8") + b"\n"
            )
    return videos, jobs, errors


def pair_files(files: list) -> tuple[list[tuple[str, bytes]], list[str], list[str]]:
    mp4 = {}
    jobs = {}
    errors = []
    for uploaded in files:
        name = uploaded.name
        if name.lower().endswith(".zip"):
            bundle_mp4, bundle_jobs, bundle_errors = parse_clip_bundle(uploaded.getvalue())
            errors.extend(f"{name}: {error}" for error in bundle_errors)
            for path, data in bundle_mp4.items():
                base = path[:-4]
                if base in mp4:
                    errors.append(f"Duplicate MP4 for {base}")
                else:
                    mp4[base] = data
            for path, data in bundle_jobs.items():
                base = path[:-9]
                if base in jobs:
                    errors.append(f"Duplicate .job.json for {base}")
                else:
                    jobs[base] = data
        elif name.lower().endswith(".mp4"):
            mp4[name[:-4]] = uploaded.getvalue()
        elif name.lower().endswith(".job.json"):
            jobs[name[:-9]] = uploaded.getvalue()

    ids = sorted(set(mp4) & set(jobs))
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
    "Clip files or ZIP bundle",
    type=["mp4", "json", "zip"],
    accept_multiple_files=True,
    help="Upload matching MP4/.job.json pairs, or a ZIP with manifest.json and its listed MP4 clips.",
)

if files:
    assets, sources, errors = pair_files(files)
    if sources:
        st.write("Ready to upload:", ", ".join(sources))
    if any(uploaded.name.lower().endswith(".zip") for uploaded in files):
        st.warning(
            "The ZIP manifest has no word-level transcript timings. Its generated jobs render without "
            "word-by-word captions, and caption quality checks may flag those clips."
        )
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
    st.info("Add matching `.mp4` and `.job.json` files, or a ZIP bundle with `manifest.json` and its MP4 clips.")
