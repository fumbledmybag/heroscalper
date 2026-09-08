FROM python:3.11-slim

WORKDIR /app

# Sin esto, Python guarda lo que imprime en un buffer de 8 KB y en Render no
# ves un log hasta que se llena: el bot parece muerto cuando está trabajando.
ENV PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p data

CMD ["python", "run.py", "bucle", "tiendas.txt"]
