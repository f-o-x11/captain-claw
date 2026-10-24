FROM python:3.11-slim

WORKDIR /app

COPY botport/pyproject.toml .
COPY botport/botport/ botport/

# "[llm]" adds litellm for LLM-assisted routing; drop it for a lean hub.
RUN pip install --no-cache-dir ".[llm]"

# Database, swarm files and (optionally) the config live on the volume.
ENV BOTPORT_DATA_DIR=/data \
    BOTPORT_WORKSPACE=/data/workspace-botport
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 23180

CMD ["botport"]
