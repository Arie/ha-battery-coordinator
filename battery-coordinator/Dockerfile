ARG BUILD_FROM
FROM $BUILD_FROM

# Use Alpine's pre-built aiohttp to avoid compiling C extensions inside
# the add-on build (which fails on armv7/aarch64 without musl-dev + gcc).
RUN apk add --no-cache python3 py3-aiohttp

COPY app/ /app/
COPY run.sh /
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
