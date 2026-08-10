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
    fonts-liberation2 \
    fonts-comfortaa \
    fonts-cabin \
    fonts-noto-core \
    fonts-comic-neue \
    # Extra display/script/serif families for the subtitle font picker — every
    # package below was downloaded and its .ttf/.otf name table was actually
    # read (fontTools) to confirm the real family name before it went in this
    # list, since Debian's fonts-* package names don't always match the font
    # inside (e.g. fonts-lobster is a mislabeled duplicate of Lobster Two —
    # deliberately left out).
    fonts-b612 \
    fonts-cabinsketch \
    fonts-cantarell \
    fonts-cardo \
    fonts-clear-sans \
    fonts-courier-prime \
    fonts-crosextra-caladea \
    fonts-crosextra-carlito \
    fonts-dancingscript \
    fonts-dosis \
    fonts-ebgaramond \
    fonts-jura \
    fonts-karla \
    fonts-karmilla \
    fonts-kaushanscript \
    fonts-league-spartan \
    fonts-leckerli-one \
    fonts-lemonada \
    fonts-linuxlibertine \
    fonts-lobstertwo \
    fonts-manrope \
    fonts-national-park \
    fonts-oxygen \
    fonts-play \
    fonts-quattrocento \
    fonts-quicksand \
    fonts-roboto-slab \
    fonts-sora \
    fonts-tuffy \
    fonts-vollkorn \
    fonts-yanone-kaffeesatz \
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
