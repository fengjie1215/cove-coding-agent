FROM redis:7.4

RUN apt-get update \
    && apt-get install --no-install-recommends -y git python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY corecoder/ ./corecoder/
COPY evals/ ./evals/
RUN python3 -m venv /opt/eval-venv \
    && /opt/eval-venv/bin/python -m pip install --no-cache-dir . \
    && mkdir -p /app/evals/results \
    && chown -R 65534:65534 /app/evals/results

ENV PATH=/opt/eval-venv/bin:$PATH \
    HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1
USER 65534:65534

CMD ["python", "-m", "evals.run", "--mock", "--pilot"]
