import asyncio
import io
import os
import re
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st
from autogen_agentchat.base import TaskResult
from autogen_agentchat.messages import TextMessage
from dotenv import load_dotenv

from config.constants import WORK_DIR_DOCKER
from config.docker_util import (
    getDockerCommandLineCodeExecutor,
    start_docker_container,
    stop_docker_container,
)
from models.openai_model_client import get_model_client
from teams.analyzer_gpt import GetDataAnalyzerTeam

load_dotenv()

README_GETTING_STARTED = (
    "https://github.com/Geekynerd1605/AnalyserGPT#getting-started"
)

st.set_page_config(
    page_title="AnalyserGPT",
    page_icon="📊",
    layout="wide",
)

WORK_DIR = Path(WORK_DIR_DOCKER)
PREVIEW_ROWS = 10
DATA_CSV_NAME = "data.csv"

SAMPLE_PROMPTS = [
    "Summarize the columns, dtypes, and basic statistics.",
    "How many missing values does each column have?",
    "Show a correlation heatmap.",
    "Create a useful bar chart from the data.",
]

CHAT_ROLES = {
    "user": ("user", "👤", "#2563eb"),
    "analyzer": ("Data Analyzer", "🤖", "#7c3aed"),
    "executor": ("Code Executor", "💻", "#059669"),
    "system": ("System", "ℹ️", "#64748b"),
}

CODE_BLOCK_RE = re.compile(r"```(\w*)\s*\n(.*?)```", re.DOTALL)
PNG_MENTION_RE = re.compile(r"([A-Za-z0-9_.-]+\.png)", re.IGNORECASE)


def _inject_theme():
    st.markdown(
        """
        <style>
        .analyser-header {
            background: linear-gradient(90deg, #1e3a5f 0%, #2d6a9f 55%, #3b82f6 100%);
            padding: 1.1rem 1.4rem;
            border-radius: 12px;
            margin-bottom: 0.75rem;
            color: #f8fafc;
        }
        .analyser-header h1 { color: #f8fafc !important; margin: 0; font-size: 1.75rem; }
        .analyser-header p { color: #e2e8f0; margin: 0.35rem 0 0 0; font-size: 0.95rem; }
        .analyser-dataset-badge {
            display: inline-block;
            background: rgba(255,255,255,0.15);
            padding: 0.25rem 0.65rem;
            border-radius: 999px;
            font-size: 0.85rem;
            margin-top: 0.5rem;
        }
        div[data-testid="stChatMessage"] {
            border-radius: 10px;
            padding: 0.15rem 0.25rem;
        }
        .msg-timestamp { color: #64748b; font-size: 0.75rem; margin-bottom: 0.25rem; }
        .role-badge {
            display: inline-block;
            font-size: 0.7rem;
            font-weight: 600;
            padding: 0.1rem 0.45rem;
            border-radius: 4px;
            margin-bottom: 0.35rem;
            color: #fff;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _init_session_state():
    defaults = {
        "messages": [],
        "autogen_team_state": None,
        "docker_executor": None,
        "docker_running": False,
        "csv_bytes": None,
        "csv_name": None,
        "model_client": None,
        "pending_prompt": None,
        "uploader_key": 0,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def _now_timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _append_message(role: str, content: str, images: list | None = None):
    entry = {
        "role": role,
        "content": content,
        "timestamp": _now_timestamp(),
    }
    if images:
        entry["images"] = images
    st.session_state.messages.append(entry)


def _role_badge_html(role_key: str) -> str:
    label, _, color = CHAT_ROLES[role_key]
    return f'<span class="role-badge" style="background:{color};">{label}</span>'


def _render_timestamp(ts: str | None):
    if ts:
        st.markdown(f'<p class="msg-timestamp">{ts}</p>', unsafe_allow_html=True)


def _split_content_and_code(content: str) -> tuple[str, list[tuple[str, str]]]:
    """Return prose (analysis/plan) and code blocks separately."""
    prose_parts: list[str] = []
    code_blocks: list[tuple[str, str]] = []
    last_end = 0
    for match in CODE_BLOCK_RE.finditer(content):
        if match.start() > last_end:
            prose_parts.append(content[last_end : match.start()])
        lang = match.group(1) or "python"
        code_blocks.append((lang, match.group(2).rstrip()))
        last_end = match.end()
    if last_end < len(content):
        prose_parts.append(content[last_end:])
    prose = "\n\n".join(p.strip() for p in prose_parts if p.strip())
    return prose, code_blocks


def _render_formatted_content(content: str):
    prose, code_blocks = _split_content_and_code(content)
    if prose:
        st.markdown(prose)
    for lang, code in code_blocks:
        if not code:
            continue
        with st.expander("View generated code", expanded=False):
            st.code(code, language=lang if lang else "python")


def _chat_message(role_key: str):
    label, avatar, _ = CHAT_ROLES[role_key]
    return st.chat_message(label, avatar=avatar)


def _resolve_chart_path(path: str) -> Path | None:
    p = Path(path)
    if not p.is_absolute():
        p = WORK_DIR / p
    if p.exists():
        return p.resolve()
    return None


def _render_chart_image(path: str, caption: str, key_prefix: str):
    resolved = _resolve_chart_path(path)
    if resolved is None:
        st.warning(f"Chart file not found: {path}")
        return
    st.image(str(resolved), caption=caption)
    img_bytes = resolved.read_bytes()
    st.download_button(
        label=f"Download {caption}",
        data=img_bytes,
        file_name=resolved.name,
        mime="image/png",
        key=f"{key_prefix}_{resolved.name}",
    )


def _render_message_body(msg: dict, msg_index: int):
    _render_timestamp(msg.get("timestamp"))
    st.markdown(_role_badge_html(msg["role"]), unsafe_allow_html=True)
    if msg.get("content"):
        _render_formatted_content(msg["content"])
    for j, img in enumerate(msg.get("images", [])):
        path = img["path"]
        caption = img.get("caption", Path(path).name)
        _render_chart_image(path, caption, f"dl_hist_{msg_index}_{j}")


def _render_chat_history():
    for i, msg in enumerate(st.session_state.messages):
        with _chat_message(msg["role"]):
            _render_message_body(msg, i)


def _render_header():
    dataset_line = ""
    if st.session_state.csv_name:
        dataset_line = (
            f'<span class="analyser-dataset-badge">'
            f"Analyzing: {st.session_state.csv_name}</span>"
        )
    st.markdown(
        f"""
        <div class="analyser-header">
            <h1>📊 AnalyserGPT</h1>
            <p>Upload a CSV, ask in plain English, and get AI-driven analysis in Docker.</p>
            {dataset_line}
        </div>
        """,
        unsafe_allow_html=True,
    )


def _check_openai_api_key() -> str | None:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key or key == "your_openai_api_key_here":
        return (
            "**OpenAI API key is missing.** Copy `.env.example` to `.env` and set "
            "`OPENAI_API_KEY`."
        )
    return None


def _check_docker_available() -> str | None:
    try:
        import docker

        client = docker.from_env()
        client.ping()
        return None
    except Exception:
        return (
            "**Docker is not running or not reachable.** Start Docker Desktop and wait "
            "until it is ready."
        )


def _check_csv_valid() -> str | None:
    if st.session_state.csv_bytes is None:
        return "**No CSV uploaded.** Upload a file on the left to start."
    try:
        df = _load_preview_df()
    except Exception:
        return "**Could not read the CSV.** Check that the file is valid UTF-8 CSV."
    if df is None or df.empty:
        return "**The CSV is empty.** Upload a file with at least one row of data."
    if len(df.columns) == 0:
        return "**The CSV has no columns.** Check the file format."
    return None


def _show_friendly_error(message: str):
    st.error(message)
    st.markdown(
        f"See the [Getting Started]({README_GETTING_STARTED}) section in the README "
        "for setup steps."
    )


def _validate_before_analysis() -> bool:
    for check in (_check_openai_api_key, _check_docker_available, _check_csv_valid):
        err = check()
        if err:
            _show_friendly_error(err)
            return False
    return True


def _persist_upload(uploaded_file):
    new_bytes = uploaded_file.getvalue()
    new_name = uploaded_file.name
    if (
        st.session_state.csv_bytes is not None
        and (new_bytes != st.session_state.csv_bytes or new_name != st.session_state.csv_name)
    ):
        st.session_state.autogen_team_state = None
        st.session_state.messages = []
    st.session_state.csv_bytes = new_bytes
    st.session_state.csv_name = new_name


def _load_preview_df() -> pd.DataFrame | None:
    if st.session_state.csv_bytes is None:
        return None
    return pd.read_csv(io.BytesIO(st.session_state.csv_bytes))


def _write_csv_to_workdir():
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    with open(WORK_DIR / DATA_CSV_NAME, "wb") as f:
        f.write(st.session_state.csv_bytes)


def _build_task(user_task: str) -> str:
    original = st.session_state.csv_name or DATA_CSV_NAME
    return (
        f"The dataset is in the working directory as `{DATA_CSV_NAME}` "
        f"(uploaded as `{original}`). "
        f"Always load it with pd.read_csv('{DATA_CSV_NAME}').\n\n"
        f"User question: {user_task}"
    )


def _snapshot_charts() -> dict[str, float]:
    """Map of absolute PNG path -> mtime before/after a run."""
    snapshot: dict[str, float] = {}
    if not WORK_DIR.exists():
        return snapshot
    for png in WORK_DIR.glob("*.png"):
        try:
            if png.stat().st_size > 0:
                snapshot[str(png.resolve())] = png.stat().st_mtime
        except OSError:
            continue
    return snapshot


def _charts_changed_since_snapshot(before: dict[str, float]) -> list[str]:
    """New or overwritten PNGs compared to a pre-run snapshot."""
    changed: list[str] = []
    if not WORK_DIR.exists():
        return changed
    for png in WORK_DIR.glob("*.png"):
        path = str(png.resolve())
        try:
            stat = png.stat()
            if stat.st_size == 0:
                continue
            if path not in before or stat.st_mtime > before[path] + 0.05:
                changed.append(path)
        except OSError:
            continue
    return sorted(changed)


def _get_attached_chart_paths() -> set[str]:
    attached: set[str] = set()
    for msg in st.session_state.messages:
        for img in msg.get("images", []):
            resolved = _resolve_chart_path(img["path"])
            if resolved:
                attached.add(str(resolved))
    return attached


def _pngs_mentioned_in_recent_messages() -> list[str]:
    """Fallback: attach PNGs the analyzer named if they exist on disk."""
    attached = _get_attached_chart_paths()
    mentioned: list[str] = []
    for msg in reversed(st.session_state.messages):
        if msg["role"] != "analyzer" or msg.get("images"):
            continue
        for match in PNG_MENTION_RE.finditer(msg.get("content", "")):
            resolved = _resolve_chart_path(match.group(1))
            if resolved:
                path = str(resolved)
                if path not in attached and path not in mentioned:
                    mentioned.append(path)
        if mentioned:
            break
    return sorted(mentioned)


def _discover_charts_after_run(chart_snapshot_before: dict[str, float]) -> list[str]:
    time.sleep(0.3)  # allow Docker volume to flush PNGs to host
    changed = _charts_changed_since_snapshot(chart_snapshot_before)
    attached = _get_attached_chart_paths()
    new_charts = [p for p in changed if p not in attached]
    if new_charts:
        return new_charts
    return [p for p in _pngs_mentioned_in_recent_messages() if p not in attached]


def _attach_charts_to_chat(chart_paths: list[str]):
    if not chart_paths:
        return
    resolved: list[str] = []
    attached = _get_attached_chart_paths()
    for path in chart_paths:
        p = _resolve_chart_path(path)
        if p and str(p) not in attached and str(p) not in resolved:
            resolved.append(str(p))
    if not resolved:
        return

    if len(resolved) == 1:
        name = Path(resolved[0]).name
        _append_message(
            "analyzer",
            f"Generated chart: `{name}`",
            images=[{"path": resolved[0], "caption": name}],
        )
    else:
        images = [{"path": p, "caption": Path(p).name} for p in resolved]
        names = ", ".join(f"`{Path(p).name}`" for p in resolved)
        _append_message(
            "analyzer",
            f"**Generated charts ({len(resolved)}):** {names}",
            images=images,
        )


def _executor_is_running(executor) -> bool:
    return executor is not None and getattr(executor, "_running", False)


async def _ensure_docker_started(status=None):
    executor = st.session_state.docker_executor
    if executor is None:
        st.session_state.docker_executor = getDockerCommandLineCodeExecutor()
        executor = st.session_state.docker_executor

    if not _executor_is_running(executor):
        if status:
            status.write("Starting Docker container…")
        await start_docker_container(executor)

    st.session_state.docker_running = _executor_is_running(executor)
    return st.session_state.docker_executor


async def _stop_docker_session():
    executor = st.session_state.docker_executor
    if executor is not None and _executor_is_running(executor):
        await stop_docker_container(executor)
    st.session_state.docker_executor = None
    st.session_state.docker_running = False


def _reset_session():
    asyncio.run(_stop_docker_session())
    st.session_state.messages = []
    st.session_state.autogen_team_state = None
    st.session_state.csv_bytes = None
    st.session_state.csv_name = None
    st.session_state.model_client = None
    st.session_state.pending_prompt = None
    st.session_state.uploader_key += 1
    data_csv = WORK_DIR / DATA_CSV_NAME
    if data_csv.exists():
        data_csv.unlink()


def _source_to_role(source: str) -> str:
    if source.startswith("user"):
        return "user"
    if source.startswith("DataAnalyzerAgent"):
        return "analyzer"
    if source.startswith("CodeExecutorAgent"):
        return "executor"
    return "system"


async def run_analyzer_gpt(model_client, task: str, status=None):
    docker = await _ensure_docker_started(status)

    if status:
        status.update(label="Running agents…", state="running")
        status.write("Data Analyzer and Code Executor are working on your question…")

    team = GetDataAnalyzerTeam(docker, model_client)

    if st.session_state.autogen_team_state is not None:
        await team.load_state(st.session_state.autogen_team_state)

    async for message in team.run_stream(task=task):
        if isinstance(message, TextMessage):
            role = _source_to_role(message.source)
            _append_message(role, message.content)
            with _chat_message(role):
                _render_message_body(
                    {"role": role, "content": message.content, "timestamp": _now_timestamp()},
                    len(st.session_state.messages),
                )
        elif isinstance(message, TaskResult):
            stop_text = f"**Stop reason:** {message.stop_reason}"
            _append_message("system", stop_text)
            with _chat_message("system"):
                _render_message_body(
                    {"role": "system", "content": stop_text, "timestamp": _now_timestamp()},
                    len(st.session_state.messages),
                )

    st.session_state.autogen_team_state = await team.save_state()

    if status:
        status.update(label="Agents finished", state="running")
        status.write("Preparing results…")


def _role_label(role_key: str) -> str:
    return CHAT_ROLES[role_key][0]


def _export_conversation(fmt: str) -> str:
    lines: list[str] = []
    dataset = st.session_state.csv_name or "—"
    exported_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if fmt == "md":
        lines.extend(
            [
                "# AnalyserGPT — Conversation Export",
                "",
                f"- **Dataset:** `{dataset}`",
                f"- **Exported:** {exported_at}",
                "",
                "---",
                "",
            ]
        )
        for msg in st.session_state.messages:
            label = _role_label(msg["role"])
            ts = msg.get("timestamp", "")
            lines.append(f"## {label} ({ts})")
            lines.append("")
            if msg.get("content"):
                lines.append(msg["content"])
                lines.append("")
            for img in msg.get("images", []):
                name = img.get("caption", Path(img["path"]).name)
                lines.append(f"**Chart:** `{name}`")
                lines.append("")
    else:
        lines.extend(
            [
                "AnalyserGPT — Conversation Export",
                f"Dataset: {dataset}",
                f"Exported: {exported_at}",
                "=" * 60,
                "",
            ]
        )
        for msg in st.session_state.messages:
            label = _role_label(msg["role"])
            ts = msg.get("timestamp", "")
            lines.append(f"[{label}] ({ts})")
            lines.append("-" * 40)
            if msg.get("content"):
                lines.append(msg["content"])
                lines.append("")
            for img in msg.get("images", []):
                name = img.get("caption", Path(img["path"]).name)
                lines.append(f"[Chart] {name}")
                lines.append("")

    return "\n".join(lines).strip() + "\n"


def _render_export_buttons():
    if not st.session_state.messages:
        return
    st.divider()
    st.markdown("**Export chat**")
    md_data = _export_conversation("md")
    txt_data = _export_conversation("txt")
    st.download_button(
        "Download as Markdown (.md)",
        data=md_data,
        file_name="analysergpt_chat.md",
        mime="text/markdown",
        use_container_width=True,
    )
    st.download_button(
        "Download as text (.txt)",
        data=txt_data,
        file_name="analysergpt_chat.txt",
        mime="text/plain",
        use_container_width=True,
    )


def _render_data_preview():
    df = _load_preview_df()
    if df is None:
        return
    st.markdown(f"**Shape:** {df.shape[0]:,} rows × {df.shape[1]} columns")
    st.markdown(f"**Columns:** {', '.join(df.columns.astype(str))}")
    st.dataframe(df.head(PREVIEW_ROWS), use_container_width=True)


def _render_sample_prompts():
    st.markdown("**Example questions**")
    for i, prompt in enumerate(SAMPLE_PROMPTS):
        if st.button(prompt, key=f"sample_prompt_{i}", use_container_width=True):
            st.session_state.pending_prompt = prompt
            st.rerun()


_init_session_state()
_inject_theme()

with st.sidebar:
    st.header("Session")
    if _executor_is_running(st.session_state.docker_executor):
        st.success("Docker container is running")
    else:
        st.caption("Docker starts on your first analysis message")
    if st.button(
        "New session",
        help="Clear chat, uploaded file, stop Docker, and reset agents",
    ):
        _reset_session()
        st.rerun()
    st.markdown(f"[Setup guide]({README_GETTING_STARTED})")
    _render_export_buttons()

_render_header()

st.subheader("Data")
uploaded_file = st.file_uploader(
    "Upload a CSV file",
    type=["csv"],
    key=f"csv_uploader_{st.session_state.uploader_key}",
)
if uploaded_file is not None:
    _persist_upload(uploaded_file)
elif st.session_state.csv_name:
    st.caption(f"Using: **{st.session_state.csv_name}**")

has_csv = st.session_state.csv_bytes is not None
has_asked = len(st.session_state.messages) > 0

if has_csv:
    csv_err = _check_csv_valid()
    if csv_err:
        st.warning(csv_err.replace("**", ""))
    else:
        _render_data_preview()
        if not has_asked:
            st.divider()
            _render_sample_prompts()
else:
    st.info("Upload a CSV to start.")

st.divider()
st.subheader("Analysis chat")

if not has_csv:
    st.info("Upload a CSV to start.")
elif not has_asked:
    st.info("Ask a question about your data — or pick an example above.")

_render_chat_history()
task = st.chat_input("Ask a question about your data...")

if st.session_state.pending_prompt:
    task = st.session_state.pending_prompt
    st.session_state.pending_prompt = None

if task:
    if not _validate_before_analysis():
        pass
    else:
        _write_csv_to_workdir()
        if st.session_state.model_client is None:
            st.session_state.model_client = get_model_client()

        chart_snapshot_before = _snapshot_charts()
        agent_task = _build_task(task)

        try:
            for attempt in range(2):
                try:
                    with st.status("Analyzing your data…", expanded=True) as status:
                        if attempt == 1:
                            status.write("Restarting Docker container…")
                        asyncio.run(
                            run_analyzer_gpt(
                                st.session_state.model_client,
                                agent_task,
                                status=status,
                            )
                        )
                        status.update(label="Checking for charts…", state="running")
                        status.write("Scanning for generated plots…")
                        new_charts = _discover_charts_after_run(chart_snapshot_before)
                        if new_charts:
                            status.write(
                                f"Attaching {len(new_charts)} chart(s) to the conversation…"
                            )
                            _attach_charts_to_chat(new_charts)
                        else:
                            status.write("No new chart files detected in tmp/.")
                        status.update(label="Analysis complete", state="complete")
                    break
                except Exception as e:
                    err_lower = str(e).lower()
                    if attempt == 0 and "container is not running" in err_lower:
                        st.session_state.docker_executor = None
                        st.session_state.docker_running = False
                        continue
                    raise
        except Exception as e:
            err_text = str(e).lower()
            st.session_state.docker_running = False
            if "container is not running" in err_text:
                st.session_state.docker_executor = None
                _show_friendly_error(
                    "**Docker session expired.** Click **New session** or ask again — "
                    "the container will restart automatically."
                )
            elif "docker" in err_text or "connection" in err_text:
                _show_friendly_error(
                    "**Docker error during analysis.** Ensure Docker Desktop is running."
                )
            elif "api" in err_text or "openai" in err_text or "auth" in err_text:
                _show_friendly_error(
                    "**OpenAI API error.** Check your API key and billing in `.env`."
                )
            else:
                _show_friendly_error(f"**Something went wrong:** {e}")
        else:
            st.rerun()
