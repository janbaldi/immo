FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md alembic.ini ./
COPY immo_agent ./immo_agent
COPY config ./config
RUN pip install uv && uv pip install --system -e ".[llm]"
EXPOSE 8000
CMD ["immo", "web"]
