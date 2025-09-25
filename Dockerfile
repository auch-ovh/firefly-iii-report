ARG PYTHON_VERSION=3.13.7
FROM python:${PYTHON_VERSION}-alpine

ENV PYTHONDONTWRITEBYTECODE=1  \
  PYTHONUNBUFFERED=1 \
  PATH=/opt/venv/bin:/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# create directories first
RUN mkdir -p /app /opt/venv /home/appuser

# create group and user (separate step for easier debugging)
RUN addgroup -S appuser && adduser -S -G appuser -h /home/appuser -s /bin/sh appuser

# create virtualenv (separate step so failures are obvious)
RUN python -m venv /opt/venv

# quick check: show python version inside venv
RUN /opt/venv/bin/python -V

# ensure correct ownership
RUN chown -R appuser:appuser /app /opt/venv /home/appuser

WORKDIR /app

# copy requirements and install as root into the venv
COPY requirements.txt .
RUN /opt/venv/bin/pip install --upgrade pip && \
  /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# copy application and set ownership to non-root user
COPY . .
RUN chown -R appuser:appuser /app

USER appuser

CMD ["/opt/venv/bin/python", "/app/monthly-report.py"]
# CMD ["sh", "-c", "echo \"0 12 1 * * /usr/bin/python3 /app/monthly-report.py\" | crontab - && exec crond -f -L /dev/stdout"]
