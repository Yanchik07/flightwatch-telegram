FROM python:3.13-slim
WORKDIR /app
COPY flightwatch.py config.json ./
COPY resources/ ./resources/
RUN useradd --uid 10001 --create-home monitor && mkdir data && chown monitor data
USER monitor
CMD ["python", "-u", "flightwatch.py", "--watch", "--send", "--update-latest"]
