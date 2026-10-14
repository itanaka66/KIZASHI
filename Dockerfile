FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY kizashi ./kizashi
RUN pip install --no-cache-dir . && useradd --system --uid 10001 kizashi \
    && mkdir -p /var/lib/kizashi/models && chown kizashi /var/lib/kizashi/models

USER kizashi
EXPOSE 9850
ENTRYPOINT ["kizashi"]
CMD ["serve", "--config", "/etc/kizashi/kizashi.yaml"]
