FROM python:3.12.12-slim@sha256:f3fa41d74a768c2fce8016b98c191ae8c1bacd8f1152870a3f9f87d350920b7c AS build

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.9.13
ARG TARGETARCH
RUN case "$TARGETARCH" in \
      amd64) checksum=41c8b0698ad3003cb5c44bde672a1ffd5f818595abd80162fbf8cc999418446a ;; \
      arm64) checksum=a165413209aab98bfb1db9629b97bc9c59778d38bb7378a33a0363cf822e7965 ;; \
      *) echo "Unsupported architecture" >&2; exit 1 ;; \
    esac \
    && curl --fail --silent --show-error --location \
      "https://github.com/cli/cli/releases/download/v2.62.0/gh_2.62.0_linux_${TARGETARCH}.tar.gz" \
      -o /tmp/gh.tar.gz \
    && echo "$checksum  /tmp/gh.tar.gz" | sha256sum --check \
    && tar -xzf /tmp/gh.tar.gz -C /tmp \
    && install -m 0755 "/tmp/gh_2.62.0_linux_${TARGETARCH}/bin/gh" /usr/local/bin/gh \
    && rm -rf /tmp/gh*
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12.12-slim@sha256:f3fa41d74a768c2fce8016b98c191ae8c1bacd8f1152870a3f9f87d350920b7c AS runtime
RUN groupadd --gid 10001 helheim \
    && useradd --uid 10001 --gid 10001 --home-dir /var/lib/helheim --no-create-home helheim \
    && install -d -o 10001 -g 10001 -m 0700 /var/lib/helheim
COPY --from=build /usr/local/bin/gh /usr/local/bin/gh
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:${PATH}" HELHEIM_HOME="/var/lib/helheim" \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
USER 10001:10001
ENTRYPOINT ["helheim"]
