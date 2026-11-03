# AIOps engine：detector（④⑤⑨）、investigator（⑥⑦⑧）、feedback_web（⑧）、scheduler（定期工作的排程器）都在同一個 image，
# 用 python3 -m <套件> <指令> 跑；設定檔（configs/）一起放進去，要改就用 chart 的 configOverrides 蓋在 /app/configs 上。
FROM python:3.13-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=Asia/Taipei
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt     # 內網：改成 --index-url 指到 Nexus
COPY core/ core/
COPY sources/ sources/
COPY models/ models/
COPY detector/ detector/
COPY investigator/ investigator/
COPY feedback_web/ feedback_web/
COPY scheduler/ scheduler/
COPY configs/ configs/
COPY interfaces/ interfaces/
COPY fake-data/ fake-data/
RUN useradd -r -u 10001 aiops
USER 10001
ENTRYPOINT ["python3", "-m"]
CMD ["detector", "--help"]
