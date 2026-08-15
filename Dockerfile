FROM python:3.12-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY worker ./worker
COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

# SERVICE_ROLE=worker runs the fan-out worker loop instead of the API;
# lets the same image serve both Railway services (see entrypoint.sh).
CMD ["./entrypoint.sh"]
