FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
RUN useradd --system --uid 10002 turno && mkdir /dados && chown turno /dados
USER turno

ENV DATABASE_URL=sqlite:////dados/turno.db
EXPOSE 8000
# Só é alcançado pelo proxy (Traefik) ou por 127.0.0.1, então confia nos cabeçalhos dele.
# Um único processo: o verificador de atrasos roda dentro dele.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
