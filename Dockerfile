FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fontconfig \
    fonts-bebas-neue \
    fonts-open-sans \
    fonts-roboto-unhinted \
    fonts-lato \
    fonts-inter \
    fonts-dejavu-core \
    fonts-liberation \
    fonts-liberation2 \
    fonts-comfortaa \
    fonts-cabin \
    fonts-noto-core \
    fonts-noto-extra \
    fonts-noto-cjk \
    fonts-noto-cjk-extra \
    fonts-noto-ui-core \
    fonts-noto-ui-extra \
    fonts-comic-neue \
    # Extra display/script/serif families for the subtitle font picker. The
    # API reads their real family names from fontconfig after installation,
    # so Debian package names never leak into the user-facing catalogue.
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
    fonts-montserrat \
    fonts-cinzel \
    fonts-playfair-display \
    fonts-raleway \
    fonts-oxygen \
    fonts-ubuntu \
    fonts-freefont-ttf \
    fonts-urw-base35 \
    fonts-texgyre \
    fonts-firacode \
    fonts-hack \
    fonts-inconsolata \
    fonts-jetbrains-mono \
    fonts-national-park \
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
