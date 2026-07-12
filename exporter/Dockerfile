FROM python:3.12-slim

# bake the DuckDB extensions into the image so pipeline runs don't depend on
# extensions.duckdb.org being reachable at runtime
RUN pip install --no-cache-dir "duckdb>=1.1,<2" "psycopg[binary]>=3.1,<4" "pyyaml>=6,<7" \
 && python -c "import duckdb; c = duckdb.connect(); c.execute('INSTALL httpfs'); c.execute('INSTALL postgres')"

WORKDIR /app
COPY export.py config.yml ./
COPY sql/ ./sql/

ENTRYPOINT ["python", "export.py"]
