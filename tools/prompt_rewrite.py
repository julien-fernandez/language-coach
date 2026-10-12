"""Rewrite a raw prompt per an editable rubric, streaming Ollama-cloud tokens to a file.

CLI helper for the prompt-reformulation window (issue #13 / #12). Reads a prompt
(file or stdin), a cloud model name, and an output path; loads a Markdown rubric
(system prompt) fresh each call; calls Ollama cloud and writes tokens to the output
file as they arrive so another process can tail it during generation.

Exit code != 0 on any failure — an empty output file is never passed off as success.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_RUBRIC = Path(__file__).resolve().parent / "prompt_rewrite_rubric.md"
MODEL_GUIDANCE = Path(__file__).resolve().parent / "prompt_rewrite_model_guidance.md"


def wcm_secret(target: str) -> str | None:
    """Read a generic credential from Windows Credential Manager (ctypes CredReadW)."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        advapi32.CredReadW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(CREDENTIAL)),
        ]
        advapi32.CredReadW.restype = wintypes.BOOL
        advapi32.CredFree.argtypes = [ctypes.c_void_p]
        ptr = ctypes.POINTER(CREDENTIAL)()
        if not advapi32.CredReadW(target, 1, 0, ctypes.byref(ptr)):  # 1 = GENERIC
            return None
        try:
            cred = ptr.contents
            if not cred.CredentialBlobSize:
                return None
            blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
            return blob.decode("utf-16-le").rstrip("\x00") or None
        finally:
            advapi32.CredFree(ptr)
    except OSError:
        return None


def linux_secret(service: str, account: str) -> str | None:
    """Lire un secret du trousseau freedesktop via secret-tool (Linux)."""
    if sys.platform == "win32":
        return None
    import shutil
    import subprocess
    exe = shutil.which("secret-tool")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "lookup", "service", service, "account", account],
            capture_output=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.decode("utf-8", "replace").strip() or None


def cloud_chunks(model: str, system: str, prompt: str, host: str, think=None):
    """Yield content chunks from Ollama cloud's streaming /api/chat (NDJSON).

    think : None -> reglage par defaut du modele ; False/True, ou "low"|"medium"|"high"
    (niveaux propres a gpt-oss). La reflexion n'est jamais renvoyee, seulement attendue."""
    import os

    key = (os.environ.get("OLLAMA_API_KEY")
           or wcm_secret("MCP_OLLAMA_API_KEY")          # Windows Credential Manager
           or linux_secret("ollama", "api"))            # trousseau freedesktop
    if not key:
        raise RuntimeError(
            "cle Ollama introuvable (env OLLAMA_API_KEY, WCM MCP_OLLAMA_API_KEY, "
            "ou : secret-tool store --label='Ollama API' service ollama account api)"
        )
    if not model.endswith(":cloud"):
        model = f"{model}:cloud"
    body = {
        "model": model,
        "stream": True,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }
    if think is not None:
        body["think"] = think
    payload = json.dumps(body).encode()
    req = urllib.request.Request(f"{host.rstrip('/')}/api/chat", data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {key}")
    with urllib.request.urlopen(req, timeout=120) as r:
        for raw in r:
            raw = raw.strip()
            if not raw:
                continue
            obj = json.loads(raw)
            if obj.get("error"):
                raise RuntimeError(f"ollama error: {obj['error']}")
            piece = (obj.get("message") or {}).get("content", "")
            if piece:
                yield piece
            if obj.get("done"):
                break


# Fournisseurs a l'API OpenAI (/chat/completions) : URL, variable d'env, service du trousseau.
OPENAI_PROVIDERS = {
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY", "groq"),
}


def openai_chunks(model: str, system: str, prompt: str, provider: str, think=None):
    """Yield content chunks from an OpenAI-compatible streaming /chat/completions (SSE).

    think : "low"|"medium"|"high" -> reasoning_effort (gpt-oss) ; la reflexion arrive
    dans un champ a part et n'est jamais renvoyee."""
    import os

    base, env, service = OPENAI_PROVIDERS[provider]
    key = os.environ.get(env) or linux_secret(service, "api")
    if not key:
        raise RuntimeError(
            f"cle {provider} introuvable (env {env}, ou : "
            f"secret-tool store --label='{provider} API' service {service} account api)"
        )
    body = {
        "model": model,
        "stream": True,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }
    if isinstance(think, str):
        body["reasoning_effort"] = think
    req = urllib.request.Request(f"{base}/chat/completions",
                                 data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {key}")
    # Avec le User-Agent par defaut d'urllib, Groq (derriere Cloudflare) repond 403.
    req.add_header("User-Agent", "language-coach/1.0")
    with urllib.request.urlopen(req, timeout=120) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue                      # ligne vide ou commentaire SSE (keep-alive)
            data = line[5:].strip()
            if data == "[DONE]":
                break
            obj = json.loads(data)
            if obj.get("error"):
                raise RuntimeError(f"{provider} error: {obj['error']}")
            for choice in obj.get("choices") or []:
                piece = (choice.get("delta") or {}).get("content") or ""
                if piece:
                    yield piece


def _last_agent_turn(jsonl: Path, cap: int) -> str:
    """Last assistant turn of a Pi session .jsonl, tail-capped."""
    text = last_assistant_text(jsonl).strip()
    return text[-cap:] if len(text) > cap else text


def agent_context(session_id: str, cap: int = 6000) -> str:
    """Last assistant turn for a Pi session id, '' if the .jsonl is unavailable."""
    jsonl = find_session(session_id)
    return _last_agent_turn(jsonl, cap) if jsonl else ""


PI_SESSIONS = Path.home() / ".pi" / "agent" / "sessions"


def find_session(session_id: str) -> Path | None:
    """Pi session .jsonl for an id: ~/.pi/agent/sessions/<slug>/<ts>_<id>.jsonl."""
    matches = sorted(PI_SESSIONS.glob(f"*/*{session_id}.jsonl"), key=lambda p: p.stat().st_mtime)
    return matches[-1] if matches else None


def _pi_entries(jsonl: Path):
    for line in jsonl.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except ValueError:
            continue


def last_assistant_text(jsonl: Path) -> str:
    """Text of the last assistant message (Pi format: type=message, role=assistant,
    content = blocks of thinking/text/toolCall). thinking and tool calls are dropped."""
    text = ""
    for entry in _pi_entries(jsonl):
        if entry.get("type") != "message":
            continue
        msg = entry.get("message") or {}
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, list):
            chunks = [
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
            ]
        elif isinstance(content, str):
            chunks = [content]
        else:
            chunks = []
        if chunks:
            text = "\n".join(chunks)
    return text


def detect_model(session_id: str) -> str:
    """modelId of the target Pi session's last model_change, '' if unknown."""
    jsonl = find_session(session_id)
    if not jsonl:
        return ""
    model = ""
    for entry in _pi_entries(jsonl):
        if entry.get("type") == "model_change" and entry.get("modelId"):
            model = entry["modelId"]
    return model


def model_key(model: str) -> str:
    """Map a model string to a guidance section key ('' = no match)."""
    m = model.lower()
    if "opus-5" in m or "opus5" in m:
        return "opus-5"
    if "sonnet-5" in m or "sonnet5" in m:
        return "sonnet-5"
    if "opus" in m:
        return "opus-4"
    if "sonnet" in m:
        return "sonnet-4"
    if "haiku" in m:
        return "haiku"
    return ""


def _section(md: str, key: str) -> str:
    """Body under '## <key>' up to the next '## ' heading, stripped; '' if absent."""
    out: list[str] = []
    capture = False
    for line in md.splitlines():
        if line.startswith("## "):
            capture = line[3:].strip() == key
            continue
        if capture:
            out.append(line)
    return "\n".join(out).strip()


def model_guidance(model: str) -> str:
    """Editable per-model prompt guidance for the detected target model."""
    key = model_key(model)
    if not key or not MODEL_GUIDANCE.is_file():
        return ""
    return _section(MODEL_GUIDANCE.read_text(encoding="utf-8"), key)


def wrap_with_context(prompt: str, context: str) -> str:
    """Wrap the prompt in <prompt_brut>, optionally prefixed by the agent's last
    turn in <contexte_agent> (XML tags: clear instruction/data boundary)."""
    body = f"<prompt_brut>\n{prompt}\n</prompt_brut>"
    if context:
        body = f"<contexte_agent>\n{context}\n</contexte_agent>\n\n{body}"
    return body


def stub_chunks(system: str, prompt: str):
    """No-network echo: proves prompt + rubric were read, exercises the stream writer."""
    yield "[STUB]\n"
    yield f"rubric: {system.splitlines()[0] if system.strip() else '(empty)'}\n"
    yield "prompt:\n"
    for line in prompt.splitlines(keepends=True):
        yield line


def stream_to_file(chunks, out_path: Path) -> int:
    """Write chunks to out_path as they arrive; return count of chars written."""
    written = 0
    with out_path.open("w", encoding="utf-8") as f:
        for piece in chunks:
            f.write(piece)
            f.flush()
            written += len(piece)
    return written


def read_prompt(prompt_file: str | None) -> str:
    if prompt_file:
        return Path(prompt_file).read_text(encoding="utf-8")
    return sys.stdin.read()


def _fail(msg: str, error_file: str | None) -> int:
    """Report a failure to stderr AND to the optional error file (for the AHK caller)."""
    sys.stderr.write(f"ERROR\t{msg}\n")
    if error_file:
        try:
            Path(error_file).write_text(msg, encoding="utf-8")
        except OSError:
            pass
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt-file", help="prompt path; stdin if omitted")
    parser.add_argument("--model", default="gpt-oss:120b", help="cloud model name")
    parser.add_argument("--output", required=True, help="output file (tokens streamed here)")
    parser.add_argument("--rubric", default=str(DEFAULT_RUBRIC), help="Markdown rubric path")
    parser.add_argument("--host", default="https://ollama.com", help="Ollama host")
    parser.add_argument("--context-session", help="Claude session id; injects its last agent turn as context")
    parser.add_argument("--target-model", help="target agent model; overrides auto-detection from --context-session")
    parser.add_argument("--error-file", help="failure reason is written here (for callers)")
    parser.add_argument("--stub", action="store_true", help="no-network echo mode")
    args = parser.parse_args(argv)

    err = args.error_file
    if err:
        try:
            Path(err).unlink()
        except OSError:
            pass

    rubric_path = Path(args.rubric)
    if not rubric_path.is_file():
        return _fail(f"rubrique introuvable: {rubric_path}", err)
    system = rubric_path.read_text(encoding="utf-8")

    target_model = args.target_model
    if not target_model and args.context_session:
        target_model = detect_model(args.context_session)
    guidance = model_guidance(target_model) if target_model else ""
    if guidance:
        system = f"{system}\n\n## Cible detectee : {target_model}\n\n{guidance}"

    try:
        prompt = read_prompt(args.prompt_file)
    except OSError as e:
        return _fail(f"prompt illisible: {e}", err)
    if not prompt.strip():
        return _fail("prompt vide", err)

    context = agent_context(args.context_session) if args.context_session else ""
    prompt = wrap_with_context(prompt, context)

    out_path = Path(args.output)
    chunks = (
        stub_chunks(system, prompt)
        if args.stub
        else cloud_chunks(args.model, system, prompt, args.host)
    )
    try:
        written = stream_to_file(chunks, out_path)
    except (urllib.error.URLError, RuntimeError, ValueError, OSError) as e:
        return _fail(f"echec reecriture: {type(e).__name__}: {e}", err)

    if written == 0:
        return _fail("reponse vide du modele", err)
    return 0


if __name__ == "__main__":
    sys.exit(main())
