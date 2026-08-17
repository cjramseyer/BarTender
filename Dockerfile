ARG BUILD_FROM=ghcr.io/hassio-addons/base:21.0.1
FROM $BUILD_FROM

# Install Python and dependencies
RUN apk add --no-cache python3 py3-pip

WORKDIR /app
COPY bartender/ bartender/
COPY requirements.txt requirements.txt

RUN pip3 install --no-cache-dir -r requirements.txt

COPY run.sh /run.sh
RUN chmod +x /run.sh

EXPOSE 8099 8100

CMD ["/run.sh"]
