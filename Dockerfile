FROM mambaorg/micromamba:latest

USER root

ENV PYTHONUNBUFFERED=1 \
    MPLCONFIGDIR=/tmp/matplotlib \
    DEIDECHO_PIXELMED_BRIDGE_CACHE=/tmp/deidecho_pixelmed_bridge

WORKDIR /opt/deid-echo

COPY . /opt/deid-echo

RUN chown -R mambauser:mambauser /opt/deid-echo && \
    mkdir -p "${MPLCONFIGDIR}" "${DEIDECHO_PIXELMED_BRIDGE_CACHE}" && \
    chmod 1777 "${MPLCONFIGDIR}" "${DEIDECHO_PIXELMED_BRIDGE_CACHE}"

USER mambauser

RUN micromamba create -y -n deid-echo -f environment.yml && \
    micromamba clean --all --yes

ENTRYPOINT ["micromamba", "run", "-n", "deid-echo", "python", "-m", "deidecho_run.run_echodeid"]
CMD ["--help"]
