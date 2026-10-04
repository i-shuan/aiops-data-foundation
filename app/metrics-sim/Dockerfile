# 正式環境從 Nexus 拉。本機測試時用 --build-arg BASE_IMAGE=python:3.12-slim 換掉。
ARG BASE_IMAGE=nexus.aillm.spil.com.tw/docker/python:3.12-slim
FROM ${BASE_IMAGE}

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .

USER 65534
EXPOSE 9464
CMD ["python", "-u", "main.py"]
