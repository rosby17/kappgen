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
    fonts-oxygen \
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
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Montserrat, Cinzel, and Playfair Display aren't packaged for Debian
# bookworm at all (confirmed: "Unable to locate package" — this silently
# broke every deploy since these were added to the subtitle font picker,
# since apt-get exits non-zero on a missing package and fails the whole
# build). fonts-raleway and fonts-ubuntu were also missing but unused by
# the actual font picker, so they're just dropped instead of replaced.
# Pulled directly from Google Fonts' own repo instead of apt.
RUN mkdir -p /usr/share/fonts/truetype/googlefonts-extra \
    && curl -fsSL -o /usr/share/fonts/truetype/googlefonts-extra/Montserrat.ttf \
        https://raw.githubusercontent.com/google/fonts/main/ofl/montserrat/Montserrat%5Bwght%5D.ttf \
    && curl -fsSL -o /usr/share/fonts/truetype/googlefonts-extra/Cinzel.ttf \
        https://raw.githubusercontent.com/google/fonts/main/ofl/cinzel/Cinzel%5Bwght%5D.ttf \
    && curl -fsSL -o /usr/share/fonts/truetype/googlefonts-extra/PlayfairDisplay.ttf \
        https://raw.githubusercontent.com/google/fonts/main/ofl/playfairdisplay/PlayfairDisplay%5Bwght%5D.ttf \
    && fc-cache -f

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p storage data

EXPOSE 8000

RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
