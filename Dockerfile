# owlpost MCP server (stdio). Mount your config directory at /config:
#   docker run -i --rm --mount type=bind,src="$HOME/.config/owlpost",dst=/config,readonly ghcr.io/smankoo/owlpost
FROM python:3.12-slim

LABEL io.modelcontextprotocol.server.name="io.github.smankoo/owlpost" \
      org.opencontainers.image.title="owlpost" \
      org.opencontainers.image.description="High-fidelity IMAP/SMTP MCP server for iCloud, Gmail, and other mail providers" \
      org.opencontainers.image.source="https://github.com/smankoo/owlpost" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    OWLPOST_CONFIG=/config/accounts.toml

WORKDIR /build
COPY pyproject.toml README.md LICENSE accounts.example.toml ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && rm -rf /build \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin owlpost \
    && mkdir -p /config \
    && chown owlpost:owlpost /config

USER owlpost
WORKDIR /home/owlpost

ENTRYPOINT ["owlpost"]
