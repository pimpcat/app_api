FROM tiangolo/uvicorn-gunicorn-fastapi:python3.10

# Mantener imagen ligera: gdal-bin (ya lo usaba el Atlas) + libgeos para Shapely.
# NO instalar geopandas/libgdal-dev aquí: rompe numpy/pandas del API principal.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gdal-bin libgeos-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Cartography Engine (router solo se monta si CARTOGRAPHY_ENGINE_ENABLED=true).
COPY requirements-cartography.txt /tmp/requirements-cartography.txt
RUN pip install --no-cache-dir -r /tmp/requirements-cartography.txt
