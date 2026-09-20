FROM python:3.13-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY pyproject.toml ./
# Resolve runtime extras and build tools before copying frequently changed source.
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    python -c "import tomllib; p=tomllib.load(open('pyproject.toml','rb')); r=p['build-system']['requires']+['wheel']+p['project']['dependencies']; r += [d for e in ('http','embedding','mcp','background') for d in p['project']['optional-dependencies'][e]]; print('\n'.join(dict.fromkeys(r)))" > /tmp/serein-requirements.txt \
    && pip install -r /tmp/serein-requirements.txt \
    && rm /tmp/serein-requirements.txt
COPY README.md ./
COPY src ./src
RUN pip install --no-deps --no-build-isolation . && pip check
CMD ["python", "-m", "serein", "--config", "/config/config.toml", "http", "--live", "--host", "0.0.0.0", "--port", "8011"]
