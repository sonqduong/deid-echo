FROM mambaorg/micromamba:latest

USER root

ENV PYTHONUNBUFFERED=1 \
    MPLCONFIGDIR=/tmp/matplotlib \
    DEIDECHO_PIXELMED_BRIDGE_CACHE=/tmp/deidecho_pixelmed_bridge

WORKDIR /opt/deid-echo

RUN mkdir -p /opt/deid-echo "${MPLCONFIGDIR}" "${DEIDECHO_PIXELMED_BRIDGE_CACHE}" && \
    chown mambauser:mambauser /opt/deid-echo && \
    chmod 1777 "${MPLCONFIGDIR}" "${DEIDECHO_PIXELMED_BRIDGE_CACHE}"

COPY --chown=mambauser:mambauser environment.yml setup.py README.md LICENSE /opt/deid-echo/
COPY --chown=mambauser:mambauser deid /opt/deid-echo/deid
COPY --chown=mambauser:mambauser deidecho_run /opt/deid-echo/deidecho_run
COPY --chown=mambauser:mambauser MANIFEST.in /opt/deid-echo/

USER mambauser

RUN micromamba create -y -n deid-echo -f environment.yml && \
    micromamba clean --all --yes

RUN micromamba run -n deid-echo python -m deidecho_run.run_echodeid \
    --check-runtime \
    --jpeg-baseline-backend require-pixelmed

ENTRYPOINT ["micromamba", "run", "-n", "deid-echo", "python", "-m", "deidecho_run.run_echodeid"]
CMD ["--help"]
