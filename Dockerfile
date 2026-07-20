FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY driver.json .
COPY src/ ./src/

ENV UC_CONFIG_HOME=/config
ENV UC_INTEGRATION_HTTP_PORT=9084

VOLUME ["/config"]
EXPOSE 9084

CMD ["python", "src/driver.py"]
