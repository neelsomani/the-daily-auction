import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

SYSTEM_PROMPT = """You are an HTML editor operating under strict sandbox constraints.

You will be given a COMMAND and CURRENT_HTML.

You must output only the complete updated HTML document for the same file.

Output must be raw HTML only: no markdown, no code fences, no explanation, no extra text.

Preserve everything not required to satisfy the command.

Do not request other files or external resources. CURRENT_HTML is the entire world.

If the command is underspecified, make the smallest reasonable assumption and proceed. Still output full HTML.
"""

DEFAULT_HTML_PATH = os.environ.get("HTML_EDIT_DEFAULT_PATH", "/app/app/default_index.html")


@dataclass(frozen=True)
class HtmlEditConfig:
    min_chars: int
    max_chars: int
    timeout_seconds: int
    schema_path: str


class HtmlEditError(RuntimeError):
    def __init__(self, message: str, raw_output: str | None = None) -> None:
        super().__init__(message)
        self.raw_output = raw_output


def validate_html_output(updated_html: str, min_chars: int, max_chars: int) -> None:
    if "```" in updated_html:
        raise HtmlEditError("output contains markdown fences", raw_output=updated_html)

    stripped = updated_html.lstrip()
    lowered = stripped.lower()
    if not (lowered.startswith("<!doctype html") or lowered.startswith("<html")):
        raise HtmlEditError("output does not start with html document", raw_output=updated_html)

    if "</html>" not in lowered:
        raise HtmlEditError("output missing closing </html>", raw_output=updated_html)

    if not (min_chars <= len(updated_html) <= max_chars):
        raise HtmlEditError("output length out of bounds", raw_output=updated_html)

    prefix_checks = ("here is", "sure", "updated html:")
    for prefix in prefix_checks:
        if lowered.startswith(prefix):
            raise HtmlEditError("output includes assistant preamble", raw_output=updated_html)


def atomic_write(path: str, content: str) -> int:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", delete=False, dir=directory, encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        temp_name = handle.name
    os.replace(temp_name, path)
    return len(content.encode("utf-8"))


def resolve_path(path: str) -> str:
    return os.path.realpath(os.path.abspath(path))


def hash_contents(contents: str) -> str:
    return hashlib.sha256(contents.encode("utf-8")).hexdigest()


def truncate_output(output: str, limit: int = 800) -> str:
    if len(output) <= limit:
        return output
    return output[-limit:]


def load_default_config() -> HtmlEditConfig:
    return HtmlEditConfig(
        min_chars=int(os.environ.get("HTML_EDIT_MIN_CHARS", "200")),
        max_chars=int(os.environ.get("HTML_EDIT_MAX_CHARS", "2000000")),
        timeout_seconds=int(os.environ.get("HTML_EDIT_TIMEOUT_SECONDS", "180")),
        schema_path=os.environ.get("HTML_EDIT_SCHEMA_PATH", "/app/app/html_output.schema.json"),
    )


def run_codex(command: str, workspace_dir: str, config: HtmlEditConfig) -> dict:
    if not (os.environ.get("OPENAI_API_KEY") or os.environ.get("CODEX_API_KEY")):
        raise HtmlEditError("OPENAI_API_KEY (or CODEX_API_KEY) is required")

    env = dict(os.environ)
    if env.get("OPENAI_API_KEY") and not env.get("CODEX_API_KEY"):
        env["CODEX_API_KEY"] = env["OPENAI_API_KEY"]

    if not os.path.exists(config.schema_path):
        raise HtmlEditError(f"schema not found: {config.schema_path}")

    prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        f"Edit index.html according to this instruction: {command}\n\n"
        "Rules:\n"
        "- You may only edit index.html\n"
        "- Do not create new files\n"
        "- Do not run commands\n"
        "- index.html must remain a complete valid HTML document\n"
        "- Always return the full HTML document (not a fragment)\n"
        "- Do not output explanations\n"
        "\n"
        "Respond with a JSON object with a single key 'html' containing the full HTML document.\n\n"
        f"CURRENT_HTML:\n{open(os.path.join(workspace_dir, 'index.html'), 'r', encoding='utf-8').read()}"
    )

    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json") as output_file:
        output_path = output_file.name

    try:
        result = subprocess.run(
            [
                "codex",
                "exec",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--output-schema",
                config.schema_path,
                "--output-last-message",
                output_path,
                prompt,
            ],
            cwd=workspace_dir,
            check=True,
            capture_output=True,
            text=True,
            timeout=config.timeout_seconds,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise HtmlEditError("codex exec timed out") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else ""
        message = "codex exec failed"
        if stderr:
            message = f"{message}: {stderr}"
        raise HtmlEditError(message) from exc

    try:
        with open(output_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise HtmlEditError("failed to read codex output json") from exc
    finally:
        try:
            os.remove(output_path)
        except OSError:
            pass

    html = payload.get("html") if isinstance(payload, dict) else None
    if not isinstance(html, str):
        raise HtmlEditError("codex output missing html field")

    return {
        "stdout": truncate_output(result.stdout or ""),
        "stderr": truncate_output(result.stderr or ""),
        "stdout_len": len(result.stdout or ""),
        "stderr_len": len(result.stderr or ""),
        "html": html,
    }

def run_html_edit(command: str, target_path: str, allowlisted_target_path: str) -> dict:
    resolved_target = resolve_path(target_path)
    resolved_allowlist = resolve_path(allowlisted_target_path)
    if resolved_target != resolved_allowlist:
        raise HtmlEditError("target_path not allowlisted")

    if not os.path.exists(resolved_target):
        os.makedirs(os.path.dirname(resolved_target) or ".", exist_ok=True)
        try:
            with open(DEFAULT_HTML_PATH, "r", encoding="utf-8") as handle:
                default_html = handle.read()
        except OSError as exc:
            raise HtmlEditError("default html not found") from exc
        atomic_write(resolved_target, default_html)

    config = load_default_config()
    workspace_dir = tempfile.mkdtemp(prefix="codex_ws_")
    try:
        workspace_file = os.path.join(workspace_dir, "index.html")
        with open(resolved_target, "r", encoding="utf-8") as handle:
            original_html = handle.read()
        original_hash = hash_contents(original_html)
        shutil.copyfile(resolved_target, workspace_file)
        codex_output = run_codex(command, workspace_dir, config)
        entries = os.listdir(workspace_dir)
        extra = [entry for entry in entries if entry != "index.html"]
        if extra:
            raise HtmlEditError(f"codex created unexpected files: {extra}")
        updated_html = codex_output["html"]
        updated_hash = hash_contents(updated_html)
        validate_html_output(updated_html, config.min_chars, config.max_chars)
        bytes_written = atomic_write(resolved_target, updated_html)
        return {
            "bytes_written": bytes_written,
            "original_hash": original_hash,
            "updated_hash": updated_hash,
            "changed": original_hash != updated_hash,
            "codex_stdout": codex_output["stdout"],
            "codex_stderr": codex_output["stderr"],
            "codex_stdout_len": codex_output["stdout_len"],
            "codex_stderr_len": codex_output["stderr_len"],
            "html_length": len(updated_html),
        }
    finally:
        shutil.rmtree(workspace_dir, ignore_errors=True)
