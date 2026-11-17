FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
ARG APP_UID=1000
RUN useradd --create-home --uid ${APP_UID} app
WORKDIR /app
COPY pyproject.toml alembic.ini ./
COPY src ./src
COPY migrations ./migrations
COPY scripts ./scripts
RUN chmod -R a+rX /app
RUN pip install .
USER app
CMD ["python", "-m", "mailassistant.web"]
