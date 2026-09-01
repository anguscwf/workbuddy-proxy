FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py extract_token.py token_storage.py ./

RUN mkdir -p /app/data

EXPOSE 19090

ENV PROXY_PORT=19090
ENV PROXY_API_KEY=wb-proxy-key
ENV CDP_URL=http://host.docker.internal:9222

CMD ["python", "server.py"]
