FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py quality.py index.html db_community.py listener_mastodon.py listener_bsky.py boot.py ./
CMD ["python", "boot.py"]
