# Image for both the API and the one-shot ingest job.
#
# Only the API's dependencies are installed, not the pipeline's: the
# heavy geospatial stack (rasterio, GDAL, WhiteboxTools, XGBoost) is
# build-time only, and its outputs are committed. Keeping it out makes
# this image small and its build fast.
#
# The one exception is raster2pgsql, which ingest.py shells out to for
# the FFPI raster; postgresql-client provides it.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY floodsafe/api/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY . /app

EXPOSE 8000

CMD ["uvicorn", "floodsafe.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
