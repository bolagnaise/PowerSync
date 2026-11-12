# Supply the reviewed immutable base digest from the release artifact manifest.
ARG PYTHON_IMAGE
FROM ${PYTHON_IMAGE}
ENV PYTHONPYCACHEPREFIX=/tmp/replay-bytecode PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
COPY requirements-test.txt /opt/requirements-test.txt
RUN python -m pip install --no-cache-dir -r /opt/requirements-test.txt
COPY custom_components/power_sync /opt/custom_components/power_sync
COPY scripts/regression_runner/runtime.py scripts/regression_runner/trusted_contract.py scripts/regression_runner/temporal.py scripts/regression_runner/__init__.py /opt/runner/regression_runner/
COPY scripts/private_regression/worker.py /opt/runner/worker.py
USER 65534:65534
ENTRYPOINT ["python", "-I", "-B", "/opt/runner/worker.py"]
