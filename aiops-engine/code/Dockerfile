# AIOps engine（aiops/：core／scenario／service／interface）。detector、investigator、api、scheduler、tasks 是相容入口，
# 都在同一個 image，角色由啟動指令決定（helmchart 的 mode：single 一隻 pod／distributed api＋beat＋worker）；
# 用 python3 -m <套件> <指令> 跑；設定檔（configs/）一起放進去，要改就用 chart 的 configOverrides 蓋在 /app/configs 上。
FROM python:3.13-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=Asia/Taipei
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt     # 內網：改成 --index-url 指到 Nexus
COPY aiops/ aiops/
# 相容入口：python3 -m detector／investigator／scheduler／tasks／api 轉給 aiops.interface.*
COPY detector/ detector/
COPY investigator/ investigator/
COPY scheduler/ scheduler/
COPY tasks/ tasks/
COPY api/ api/
COPY configs/ configs/
COPY interfaces/ interfaces/
COPY fake-data/ fake-data/
RUN useradd -r -u 10001 aiops
USER 10001
ENTRYPOINT ["python3", "-m"]
CMD ["detector", "--help"]
