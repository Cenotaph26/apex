FROM python:3.12-slim

WORKDIR /app

# Install deps first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Railway injects PORT env var
ENV PORT=8000

EXPOSE 8000

CMD ["python", "main.py"]
