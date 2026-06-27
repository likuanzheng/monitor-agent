FROM python:3.10-slim
RUN pip install --no-cache-dir jetson-stats==4.3.2 prometheus_client
COPY exporter.py /exporter.py
CMD ["python3", "/exporter.py"]
