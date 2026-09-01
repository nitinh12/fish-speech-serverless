# syntax=docker/dockerfile:1.7

ARG SGLANG_IMAGE=lmsysorg/sglang@sha256:9e148f5ac788e856a06166bd6347a831831eb9fcfab4d1770874823a7c29a1a1
ARG FLASHINFER_CACHE_IMAGE=hongccc/sglang-omni@sha256:02a85f00438c901c72a2eb2ef738974a807f63af3d13084445604f3344067b19

FROM ${FLASHINFER_CACHE_IMAGE} AS flashinfer-cache
FROM ${SGLANG_IMAGE} AS runtime

ARG UCX_COMMIT=d8e50df6651b9ea5b76f23aee0aefbf053a4137a

RUN apt-get update \
    && apt-get install -y --no-install-recommends sox git \
    && rm -rf /var/lib/apt/lists/*

# ---- UCX: identical to your existing image ----
RUN git clone --filter=blob:none https://github.com/openucx/ucx.git /tmp/ucx \
    && git -C /tmp/ucx checkout "${UCX_COMMIT}" \
    && cd /tmp/ucx \
    && ./autogen.sh \
    && ./contrib/configure-release-mt \
        --enable-shared --disable-static --disable-doxygen-doc \
        --enable-optimizations --enable-cma --enable-devel-headers \
        --with-cuda=/usr/local/cuda --with-verbs --with-dm \
        --prefix=/usr/local \
    && make -j"$(nproc)" \
    && make install-strip \
    && ldconfig \
    && rm -rf /tmp/ucx

# ---- sglang-omni: baked in at BUILD time instead of cloned at boot ----
RUN git clone --depth 1 --branch main \
        https://github.com/sgl-project/sglang-omni.git /opt/sglang-omni

RUN uv pip install --system --break-system-packages --no-build-isolation \
        -r /opt/sglang-omni/pyproject.toml \
    && uv pip install --system --break-system-packages --no-deps qwen-tts==0.1.1 \
    && uv pip install --system --break-system-packages --no-deps --reinstall flashinfer-python==0.6.17 \
    && python3 -m pip uninstall -y flashinfer-cubin flashinfer-jit-cache \
    && uv pip install --system --break-system-packages --no-deps --no-build-isolation \
        -e /opt/sglang-omni

# Codec deps from your pod script + the serverless worker SDK
RUN uv pip install --system --break-system-packages \
        "descript-audiotools==0.7.2" \
        "descript-audio-codec==1.0.0" \
        runpod httpx soundfile

# Prebuilt FlashInfer JIT cache — valid for SM89 (RTX 4090 / L40S) and SM90a (H100 / H200)
COPY --from=flashinfer-cache /root/.cache/flashinfer/0.6.17 /root/.cache/flashinfer/0.6.17
ENV FLASHINFER_WORKSPACE_BASE=/root \
    FLASHINFER_JIT_DEBUG=0 \
    PYTHONUNBUFFERED=1

# ---- serverless worker ----
COPY worker.py /app/worker.py

RUN <<'EOF'
cat >/usr/local/bin/fish-speech-worker <<'SCRIPT'
#!/usr/bin/env bash
set -euo pipefail
if [ -x /opt/nvidia/nvidia_entrypoint.sh ]; then
    exec /opt/nvidia/nvidia_entrypoint.sh "$@"
fi
exec "$@"
SCRIPT
chmod 0755 /usr/local/bin/fish-speech-worker
EOF

ENTRYPOINT ["/usr/local/bin/fish-speech-worker"]
CMD ["python3", "-u", "/app/worker.py"]
