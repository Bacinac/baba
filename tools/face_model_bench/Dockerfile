FROM python:3.14-slim
RUN pip install --no-cache-dir numpy opencv-python-headless onnxruntime scipy && \
    pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir --no-deps timm einops
WORKDIR /work
