FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
EXPOSE 8000
# --forwarded-allow-ips: cloudflared reaches us from a compose-network address,
# not 127.0.0.1, so uvicorn's default would ignore its X-Forwarded-Proto and
# build http:// links. Trusting any peer is safe here because port 8000 is
# never published to the host -- only the compose network can reach it.
CMD ["uvicorn", "--factory", "hyrox.app:create_app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
