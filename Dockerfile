FROM python:3.12.7-slim

RUN pip install --no-cache-dir "psycopg[binary]==3.2.3"

WORKDIR /app
COPY app.py .

USER 1000:1000
EXPOSE 8080
CMD ["python", "-u", "app.py"]
