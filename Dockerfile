# Same base image (by digest) the commits were labeled in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONUNBUFFERED=1

COPY requirements.lock /app/requirements.lock
RUN pip install -r /app/requirements.lock

# sqlglot at the pinned commit, as plain files under /app/src
COPY tools/fetch_sqlglot.py /app/fetch_sqlglot.py
RUN python /app/fetch_sqlglot.py /app/src

COPY selector.py map.json /app/
# bytecode for everything the run imports, so children never compile
RUN python -m compileall -q /app/src/sqlglot /app/src/tests /app/selector.py \
    && chmod -R a+rX /app \
    && test -f /app/src/sqlglot/parser.py && test -f /app/src/tests/test_optimizer.py

ENV BR_SRC=/app/src BR_MAP=/app/map.json
CMD ["python", "/app/selector.py"]
