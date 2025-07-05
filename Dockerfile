# Gunakan imej Python 3.11 rasmi sebagai asas
FROM python:3.11-slim

# Tetapkan direktori kerja di dalam kontena
WORKDIR /app

# Pasang kebergantungan sistem yang diperlukan oleh aplikasi anda
# Tesseract untuk OCR, Poppler untuk penukaran PDF, dan libgl1 untuk OpenCV(cv2)
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    poppler-utils \
    libgl1-mesa-glx \
    --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Salin fail requirements ke dalam kontena
COPY requirements.txt .

# Pasang pakej-pakej Python
RUN pip install --no-cache-dir -r requirements.txt

# Salin keseluruhan projek anda ke dalam kontena
COPY . .

# Cipta folder 'uploads' semasa proses build
RUN mkdir uploads

# Tentukan command untuk menjalankan aplikasi anda menggunakan Gunicorn (Production)
CMD ["gunicorn", "--bind", "0.0.0.0:10000", "app:app"]
