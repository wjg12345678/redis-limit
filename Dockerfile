FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        cmake \
        libhiredis-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir pybind11

WORKDIR /app
COPY CMakeLists.txt ./
COPY include/ include/
COPY src/ src/

RUN cmake -S . -B build \
      -Dpybind11_DIR="$(python3 -c 'import pybind11; print(pybind11.get_cmake_dir())')" \
    && cmake --build build

FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        libhiredis-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /app/build/redis_limiter*.so ./
COPY examples/ examples/

CMD ["python3", "examples/python_demo.py"]
