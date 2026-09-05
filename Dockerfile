FROM python:3.11-slim-bookworm

# Node.js 22 + the HyperFrames CLI, used in isolated overlay mode only by the
# facecam pipeline's motion-graphic title cards (facecam_cards.py) — never a
# dependency of the existing faceless render. Adopted deliberately despite
# the extra runtime (see ROADMAP.md): HyperFrames renders HTML/CSS/GSAP
# compositions via headless Chrome, which the base ffmpeg pipeline has no
# equivalent for.
#
# HyperFrames' own `browser ensure` step (previously run below, now removed)
# fetches a pinned "Chrome for Testing" build straight from Google's CDN —
# every single deploy since this was added either failed or hung for over an
# hour on it, never once completing (so Docker had nothing to cache and
# re-tried the same stuck download from scratch every time), pointing at
# that CDN being slow/blocked from this host's network. HyperFrames also
# happily runs against a plain system Chromium (it auto-detects
# /usr/bin/chromium, and HYPERFRAMES_BROWSER_PATH below makes that explicit
# rather than relying on the fallback order) — `apt-get install chromium`
# uses the same Debian mirror every other package here already installs
# from fine, and pulls in all of Chrome's shared-lib dependencies itself,
# so the long hand-picked libnss3/libatk/... list is gone too.
RUN apt-get update && apt-get install -y --no-install-recommends curl gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs chromium \
    && npm install -g hyperframes \
    && rm -rf /var/lib/apt/lists/*
ENV HYPERFRAMES_BROWSER_PATH=/usr/bin/chromium

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
    # fonts-noto-core/-ui-core only — kept purely as a broad-Unicode fallback
    # so a stray non-Latin character (an accented proper noun, a symbol) in
    # an otherwise French script doesn't render as a missing-glyph box.
    # fonts-noto-extra/-cjk/-cjk-extra/-ui-extra dropped entirely: they cover
    # scripts (Tamil, Thai, CJK, etc.) this French-language product never
    # renders, the API's font picker now excludes every Noto face outright
    # anyway (see list_render_fonts in channels.py), and they're some of the
    # largest packages in this whole install list — dead weight that was
    # only ever slowing every build down.
    fonts-noto-core \
    fonts-noto-ui-core \
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
