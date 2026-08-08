FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fontconfig \
    fonts-montserrat \
    fonts-bebas-neue \
    fonts-open-sans \
    fonts-roboto-unhinted \
    fonts-lato \
    fonts-inter \
    fonts-adobe-sourcesans3 \
    fonts-dejavu-core \
    fonts-liberation \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p storage data

EXPOSE 8000

RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
