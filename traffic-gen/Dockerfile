FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir aiohttp==3.9.5

COPY traffic_gen_v2.py .

CMD ["python", "-u", "traffic_gen_v2.py"]