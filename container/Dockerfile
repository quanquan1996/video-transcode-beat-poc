FROM public.ecr.aws/docker/library/python:3.11-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# CPU-only PyTorch keeps the image small; beat_this runs fine on CPU.
RUN pip install --no-cache-dir torch==2.5.1 torchaudio==2.5.1 --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir beat-this boto3 numpy einops soxr rotary-embedding-torch tqdm

# Bake the checkpoint (HF mirror of CPJKU beat_this final0, MIT) into the image and verify its hash.
ARG CKPT_URL=https://huggingface.co/Audante/beat-this-final0/resolve/main/beat_this-final0.ckpt
ARG CKPT_SHA256=8c328b45f59d8dd3dff219253ff6a8d6482be57d0133a29140e2febbf8eb8331
RUN mkdir -p /opt/model \
 && curl -fsSL -o /opt/model/final0.ckpt "$CKPT_URL" \
 && echo "$CKPT_SHA256  /opt/model/final0.ckpt" | sha256sum -c -

COPY worker.py /app/worker.py
ENV CKPT=/opt/model/final0.ckpt PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "/app/worker.py"]
