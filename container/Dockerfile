# ECS image (Fargate CPU tasks and GPU EC2 tasks). bin/ and model/ are prepared by buildspec.yml.
FROM public.ecr.aws/docker/library/python:3.12-slim
COPY bin/ffmpeg bin/ffprobe /usr/local/bin/
RUN pip install --no-cache-dir torch==2.5.1 torchaudio==2.5.1 --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir beat-this boto3 numpy einops soxr rotary-embedding-torch tqdm
COPY model/final0.ckpt /opt/model/final0.ckpt
COPY worker.py /app/worker.py
ENV CKPT=/opt/model/final0.ckpt PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "/app/worker.py"]
