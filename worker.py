"""
RunPod serverless worker for Fish Speech (sglang-omni / s2-pro).

Job input format:
  {
    "route":   "/v1/audio/speech",     # path on the sglang server
    "method":  "POST",                 # optional, default POST
    "payload": { ... },                # optional JSON body
    "media":   {"ref.wav": "<base64>"} # optional files written to the media dir
  }
JSON responses come back as {"status_code", "json", ...};
binary responses (audio) as {"status_code", "content_type", "data_b64", ...}.
"""

import os, sys, time, base64, subprocess

import httpx
import runpod

SGL_PORT        = int(os.environ.get("SGL_PORT", "7860"))
LOCAL_BASE      = f"http://127.0.0.1:{SGL_PORT}"
MODEL_PATH      = os.environ.get("MODEL_PATH", "fishaudio/s2-pro")
SGL_CONFIG      = os.environ.get("SGL_CONFIG", "examples/configs/s2pro_tts.yaml")
REPO_DIR        = os.environ.get("SGLANG_OMNI_HOME", "/opt/sglang-omni")
MEDIA_DIR       = os.environ.get("LOCAL_MEDIA_PATH", "/tmp/media")
STARTUP_TIMEOUT = int(os.environ.get("STARTUP_TIMEOUT", "600"))
JOB_TIMEOUT     = float(os.environ.get("JOB_TIMEOUT", "600"))


def start_backend() -> subprocess.Popen:
    cmd = [
        "sgl-omni", "serve",
        "--model-path", MODEL_PATH,
        "--config", SGL_CONFIG,
        "--port", str(SGL_PORT),
        "--cuda-graph-max-bs", os.environ.get("CUDA_GRAPH_MAX_BS", "4"),
        "--max-running-requests", os.environ.get("MAX_RUNNING_REQUESTS", "4"),
        "--max-total-tokens", os.environ.get("MAX_TOTAL_TOKENS", "32768"),
        "--allowed-local-media-path", "/tmp",
    ]
    print("Starting backend:", " ".join(cmd), flush=True)
    return subprocess.Popen(cmd, cwd=REPO_DIR)


def wait_for_backend(timeout: int) -> None:
    deadline = time.monotonic() + timeout
    with httpx.Client(timeout=5.0) as client:
        while time.monotonic() < deadline:
            try:
                r = client.get(f"{LOCAL_BASE}/health")
                if r.status_code < 500:      # even 404 = server is listening
                    print("Backend is up.", flush=True)
                    return
            except httpx.HTTPError:
                pass
            time.sleep(2.0)
    print(f"Backend not ready within {timeout}s — exiting so the worker restarts.", flush=True)
    sys.exit(1)


def handler(job):
    inp = job.get("input", {}) or {}

    # 1. Persist uploaded media (base64 -> files inside the container)
    media = inp.get("media") or {}
    local_paths = {}
    if media:
        os.makedirs(MEDIA_DIR, exist_ok=True)
        for name, b64 in media.items():
            safe = os.path.basename(str(name))
            path = os.path.join(MEDIA_DIR, safe)
            with open(path, "wb") as f:
                f.write(base64.b64decode(b64))
            local_paths[safe] = path

    # 2. Rewrite reference audio paths to the worker-local copies
    payload = inp.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("references"), list):
        for ref in payload["references"]:
            if isinstance(ref, dict) and ref.get("audio_path"):
                base = os.path.basename(str(ref["audio_path"]))
                if base in local_paths:
                    ref["audio_path"] = local_paths[base]

    route = inp.get("route", "/health")
    if not route.startswith("/"):
        route = "/" + route
    method = (inp.get("method") or "POST").upper()
    timeout = float(inp.get("timeout_s", JOB_TIMEOUT))

    try:
        r = httpx.request(method, LOCAL_BASE + route,
                          params=inp.get("params"), json=payload, timeout=timeout)
    except httpx.HTTPError as e:
        return {"error": f"backend request failed: {e}"}

    out = {"status_code": r.status_code}
    for src, dst in (("x-sample-rate", "sample_rate"),
                     ("x-channels", "channels"),
                     ("x-bit-depth", "bit_depth")):
        v = r.headers.get(src)
        if v:
            out[dst] = v

    if (r.headers.get("content-type", "") or "").startswith("application/json"):
        out["json"] = r.json()
        return out
    out["content_type"] = r.headers.get("content-type", "application/octet-stream")
    out["data_b64"] = base64.b64encode(r.content).decode("utf-8")
    return out


backend = start_backend()
wait_for_backend(STARTUP_TIMEOUT)

runpod.serverless.start(
    {
        "handler": handler,
        # Optional later: sglang batches, so one worker can take multiple jobs
        # (up to --max-running-requests). Keep 1 while testing.
        # "concurrency_modifier": lambda x: x * 4,
    }
)
