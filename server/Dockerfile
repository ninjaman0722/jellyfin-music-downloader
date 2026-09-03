FROM python:3.12-slim

RUN apt-get update && apt-get install -y ffmpeg nodejs curl unzip ca-certificates && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh
RUN pip install --no-cache-dir --upgrade pip yt-dlp spotdl

# 1. Language patch: German -> English for YouTube Music search
RUN sed -i 's/language="de"/language="en"/g' /usr/local/lib/python3.12/site-packages/spotdl/providers/audio/ytmusic.py

# 2. Instant Skip patch: check local output_file before making Spotify web API reinit calls
COPY patch_skip.py /tmp/patch_skip.py
RUN python3 /tmp/patch_skip.py && rm /tmp/patch_skip.py

WORKDIR /music
ENTRYPOINT ["spotdl"]
