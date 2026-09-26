FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY scholar_api ./scholar_api
RUN pip install --no-cache-dir .
ENV HOST=0.0.0.0 PORT=8000
EXPOSE 8000
# Keep a single worker: rate limiting and the cache live in-process.
CMD ["scholar-api"]
