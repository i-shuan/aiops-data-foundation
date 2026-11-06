# AIOps engine（aiops/：core／scenario／service／interface）。一個 image、四個角色（detector、investigator、scheduler、tasks、api），
# 角色由啟動指令決定（helmchart 的 mode：single 一隻 pod／distributed api＋beat＋worker），
# 用 python3 -m aiops <角色> <指令> 跑；設定檔（configs/）一起放進去，要改就用 chart 的 configOverrides 蓋在 /app/configs 上。
FROM python:3.13-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=Asia/Taipei
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt     # 內網：改成 --index-url 指到 Nexus
COPY aiops/ aiops/
COPY configs/ configs/
COPY interfaces/ interfaces/
COPY fake-data/ fake-data/
RUN useradd -r -u 10001 aiops
USER 10001
ENTRYPOINT ["python3", "-m"]
CMD ["aiops", "detector", "--help"]
