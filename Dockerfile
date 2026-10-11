FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .

# The bot needs no root rights. It gets a user of its own, and that user owns the data folder.
RUN useradd --uid 1000 --create-home ebbot && mkdir /data && chown ebbot:ebbot /data
USER ebbot

ENV DATA_DIR=/data PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080

CMD ["python3", "bot.py"]
