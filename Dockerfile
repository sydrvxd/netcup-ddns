FROM python:3.13-slim

WORKDIR /app
RUN pip install --no-cache-dir requests
COPY netcup_dns.py .

CMD ["python", "-u", "netcup_dns.py"]
