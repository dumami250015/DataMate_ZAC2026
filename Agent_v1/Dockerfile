# Supply the organizer-approved, digest-pinned CUDA/PyTorch image at build time.
# Example: docker build --build-arg BASE_IMAGE=<approved-image>@sha256:<digest> ...
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
WORKDIR /opt/datamate
COPY . /opt/datamate
# Build wheelhouse and requirements-runtime.lock.txt on the target platform first.
RUN python -m pip install --no-index --find-links=/opt/datamate/wheelhouse -r /opt/datamate/requirements-runtime.lock.txt \
    && chmod +x /opt/datamate/predict.sh
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_DATASETS_OFFLINE=1
ENTRYPOINT ["/opt/datamate/predict.sh"]

