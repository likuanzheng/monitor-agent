FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py config.yaml ./

# config.yaml 在 compose 里以只读卷挂载覆盖，便于改配置不重建镜像
CMD ["python", "agent.py"]
