FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Kolkata

# libgomp1: onnxruntime (CAPTCHA OCR via ddddocr, screenshot OCR via rapidocr)
# libgl1, libglib2.0-0: OpenCV, which rapidocr pulls in
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 libgl1 libglib2.0-0 tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# The OCR check makes the build fail here, not a lawyer's upload later
RUN pip install -r requirements.txt \
    && python -c "from rapidocr import RapidOCR; RapidOCR()"

COPY . .

RUN useradd --create-home --uid 1000 courtpilot && chown -R courtpilot /app
USER courtpilot

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
