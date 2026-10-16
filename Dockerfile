FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# hidapi 在 Linux 上需要 libusb 运行库。
# 本项目只用 TCP 连读卡器，但 SDK 的 uhf.reader.communication 在模块级就 import 了
# HidClient 和 BLEClient，所以 `import uhf.reader` 会连带加载 hidapi —— 不装就直接报错。
RUN apt-get update \
    && apt-get install -y --no-install-recommends libusb-1.0-0 \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖，改代码时不必重装这一层
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY tools/ ./tools/
COPY config.yaml .

RUN mkdir -p /app/data

EXPOSE 8000

CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
