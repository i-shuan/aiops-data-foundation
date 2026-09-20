FROM nexus.aillm.spil.com.tw/docker/python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .

# 不要用 root 跑
USER 65534
EXPOSE 9464
CMD ["python", "-u", "main.py"]
