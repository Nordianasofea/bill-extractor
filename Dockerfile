# Use an official Python image as the base
FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app

# Install system dependencies required by your app.py
# Tesseract for OCR and Poppler for PDF conversion
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    poppler-utils \
    --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Copy the requirements file into the container
COPY requirements.txt .

# Install Python packages
RUN pip install --no-cache-dir -r requirements.txt

# Copy your entire project into the container
COPY . .

# Define the command to run your app using Gunicorn
CMD ["gunicorn", "--bind", "0.0.0.0:10000", "app:app"]
