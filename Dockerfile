# syntax=docker/dockerfile:1.7

# Pinned to the multi-arch manifest-list (OCI index) digest for reproducibility.
ARG SWIPL_IMAGE=docker.io/library/swipl:10.0.2@sha256:f801ce1773c0b909e7ccf48aef979bf4aeab591d43ccaca68014925b904ac237

FROM ${SWIPL_IMAGE} AS builder

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ENV DEBIAN_FRONTEND=noninteractive \
    HF_HOME=/opt/huggingface \
    SENTENCE_TRANSFORMERS_HOME=/opt/sentence_transformers

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      git \
      build-essential \
      cmake \
      pkg-config \
      python3 \
      python3-dev \
      python3-pip \
      libopenblas-dev \
      libblas-dev \
      liblapack-dev \
      gfortran \
      libgflags-dev \
      nano \
 && rm -rf /var/lib/apt/lists/*

# Build dependencies from source, pinned for reproducibility.
# PeTTa and chromadb are pinned to full commit SHAs (see fetch-by-SHA clones
# below); FAISS to an immutable release tag.
ARG PETTA_REPO=https://github.com/trueagi-io/PeTTa.git
ARG PETTA_REF=d8d46920269ced70cd6236a5182d4d2409c1e12b
ARG FAISS_REPO=https://github.com/facebookresearch/faiss.git
ARG FAISS_REF=v1.8.0
ARG CHROMADB_REPO=https://github.com/patham9/petta_lib_chromadb.git
ARG CHROMADB_REF=456385457e4e99ee049c2c0966988a6cd7ff3705

# Embedding model to pre-download at build time, pinned to an immutable revision.
ARG EMBEDDING_MODEL=intfloat/e5-large-v2
ARG EMBEDDING_REVISION=f169b11e22de13617baa190a028a32f3493550b6

# PeTTa is pinned to a commit SHA; git clone --branch cannot take a SHA, so
# fetch the exact commit shallowly and detach onto it.
RUN git init /PeTTa \
 && git -C /PeTTa remote add origin "${PETTA_REPO}" \
 && git -C /PeTTa fetch --depth 1 origin "${PETTA_REF}" \
 && git -C /PeTTa checkout --detach FETCH_HEAD
RUN git clone --depth 1 --branch "${FAISS_REF}" "${FAISS_REPO}" /faiss

WORKDIR /faiss
RUN cmake -B build -DFAISS_ENABLE_GPU=OFF -DFAISS_ENABLE_PYTHON=OFF -DBUILD_SHARED_LIBS=OFF \
 && cmake --build build --config Release --parallel \
 && cmake --install build

WORKDIR /PeTTa
RUN sh build.sh
# chromadb is pinned to a commit SHA; fetch the exact commit shallowly.
RUN mkdir -p /PeTTa/repos/petta_lib_chromadb \
 && git init /PeTTa/repos/petta_lib_chromadb \
 && git -C /PeTTa/repos/petta_lib_chromadb remote add origin "${CHROMADB_REPO}" \
 && git -C /PeTTa/repos/petta_lib_chromadb fetch --depth 1 origin "${CHROMADB_REF}" \
 && git -C /PeTTa/repos/petta_lib_chromadb checkout --detach FETCH_HEAD

COPY ./requirements.txt /tmp/requirements.txt
RUN python3 -m pip install --no-cache-dir --break-system-packages \
    --index-url https://download.pytorch.org/whl/cpu \
    --extra-index-url https://pypi.org/simple/ \
    torch==2.12.1 \
 && python3 -m pip install --no-cache-dir --break-system-packages -r /tmp/requirements.txt

# Pre-download the sentence-transformers model so runtime does not need network access.
RUN mkdir -p "${HF_HOME}" "${SENTENCE_TRANSFORMERS_HOME}" \
 && python3 - <<PY
from sentence_transformers import SentenceTransformer
model_name = "${EMBEDDING_MODEL}"
revision = "${EMBEDDING_REVISION}"
print(f"Downloading embedding model: {model_name}@{revision}")
SentenceTransformer(model_name, revision=revision)
print("Model download complete.")
PY

FROM ${SWIPL_IMAGE} AS runtime

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/opt/huggingface \
    SENTENCE_TRANSFORMERS_HOME=/opt/sentence_transformers

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      python3 \
      libopenblas-dev \
      libblas-dev \
      liblapack-dev \
      gfortran \
      libgflags-dev \
      nano \
      git \
      nginx-light \
      gettext-base \
      poppler-utils \
      curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /PeTTa

COPY --from=builder /usr/local /usr/local
COPY --from=builder /PeTTa /PeTTa
COPY --from=builder /opt/huggingface /opt/huggingface
COPY --from=builder /opt/sentence_transformers /opt/sentence_transformers

# setup nginx proxy
RUN usermod -a -G tty www-data
RUN mkdir /opt/nginx
RUN chown www-data:www-data /opt/nginx
RUN chmod 0700 /opt/nginx
COPY --chown=www-data:www-data --chmod=0600 ./proxy/* /opt/nginx/

ENV OMEGACLAW_DIR=/PeTTa/repos/OmegaClaw-Core
ENV MEMORY_DIR=${OMEGACLAW_DIR}/memory
# Start defaults for import-kb
ENV IMPORT_KB_ON_START=0

# Bring in only local OmegaClaw source (filtered by .dockerignore).
COPY . ${OMEGACLAW_DIR}

RUN cp ${OMEGACLAW_DIR}/run.metta /PeTTa/run.metta \
 && mkdir -p ${MEMORY_DIR}/chroma_db \
 && ln -s ${MEMORY_DIR}/chroma_db ./chroma_db \
 && chmod +x ${OMEGACLAW_DIR}/entrypoint.sh \
 && chmod +x ${OMEGACLAW_DIR}/scripts/import_knowledge.sh \
 && chown -R 65534:65534 ${MEMORY_DIR} \
 && find ${MEMORY_DIR} -type f -exec chmod 0644 {} \; \
 && chmod 0444 ${MEMORY_DIR}/prompt.txt \
 && chown -R 65534:65534 /opt/huggingface /opt/sentence_transformers

ENTRYPOINT ["/PeTTa/repos/OmegaClaw-Core/entrypoint.sh"]
CMD []
