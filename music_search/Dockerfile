# Supervisor passes BUILD_FROM; the plain Python image is enough here.
ARG BUILD_FROM
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir "rapidfuzz==3.*" "pyyaml==6.*"
COPY music_search.py editor.html /app/

CMD ["python", "/app/music_search.py"]
