# One image, two roles (SERVICE_ROLE=api | worker). Build:  docker build -t aml-copilot --build-arg GIT_SHA=$(git rev-parse HEAD) .
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/opt/hf

WORKDIR /app

# 1. CPU-only torch FIRST. A plain `pip install sentence-transformers` on Linux pulls several GB of CUDA wheels.
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu

FROM base AS deps
# 2. Dependencies (copied alone so this layer is cached until a requirements file changes).
COPY requirements.txt requirements-core.txt ./
# uv resolves in seconds; pip's resolver backtracked for 30+ minutes on this dependency set.
RUN pip install uv && uv pip install --system -r requirements.txt

FROM deps AS model
# 3. Bake the embedding model into the image, then forbid network access to the model hub at run time.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1

FROM model AS final
# 4. Only the application code and the typology documents - nothing else from the repo (see .dockerignore).
COPY aml_copilot ./aml_copilot
COPY typology_docs ./typology_docs

# 5. Pre-build the vector index so a cold container never has to.
RUN python -m aml_copilot.retrieval build

# 6. Record which commit this image was built from (shown by /healthz and in every audit event).
ARG GIT_SHA=unknown
ENV BUILD_SHA=${GIT_SHA}
RUN printf '{"sha": "%s"}\n' "${GIT_SHA}" > /app/BUILD_INFO.json

# 7. Run as a non-root user. The app only writes to /tmp-style locations at run time.
RUN useradd --create-home --uid 10001 app && chown -R app /app
USER app

ENV SERVICE_BACKEND=gcp \
    SERVICE_ROLE=api \
    AUTH_MODE=firebase \
    LOG_FORMAT=json \
    API_PREFIX=/api

# Cloud Run supplies $PORT (8080 by default).
CMD ["sh", "-c", "exec uvicorn aml_copilot.service.app:app --host 0.0.0.0 --port ${PORT:-8080}"]
