# Grandmaster Chess Coach — web edition, for Hugging Face Spaces (Docker SDK)
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends stockfish \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY webapp/requirements.txt ./webapp/requirements.txt
RUN pip install --no-cache-dir -r webapp/requirements.txt

COPY chesscoach ./chesscoach
COPY webapp ./webapp

ENV STOCKFISH=/usr/games/stockfish \
    COACH_DATA=/tmp/coach-data

EXPOSE 7860
CMD ["uvicorn", "webapp.app:app", "--host", "0.0.0.0", "--port", "7860"]
