FROM python:3.14-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

COPY uv.lock /tmp/uv.lock
RUN python3 -c 'import tomllib; p = next(p for p in tomllib.load(open("/tmp/uv.lock", "rb"))["package"] if p["name"] == "pillow"); print("pillow==" + p["version"] + " " + " ".join("--hash=" + w["hash"] for w in p["wheels"]))' > /tmp/requirements.txt \
 && python3 -m venv /opt/tools \
 && /opt/tools/bin/pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt \
 && rm /tmp/uv.lock /tmp/requirements.txt

ENTRYPOINT ["/opt/tools/bin/python3"]
