FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./

RUN pip install --no-cache-dir -r requirements.txt

RUN mkdir -p /app/data

COPY . .

# Health = log file touched in the last 10 minutes. The connection watchdog
# (lib/bot.py) bumps the log mtime every 60s while connected, so a wedged event
# loop or a permanent network disconnect both stop the touches and trip the
# check. Combined with `restart: unless-stopped` in compose this turns silent
# wedges into automatic restarts.
#
# LOG_FILE is read at runtime so the healthcheck honors any custom value
# in .env (default `hikaru_bot.log`). os.path.exists guard prevents the
# check from crashing if the log file was deleted (FileNotFoundError on
# getmtime would otherwise mark the container unhealthy without a clean
# exit). start_period=5m gives the chained migrations time to run on
# first boot for legacy DBs — table rebuilds on a large reading_logs
# can exceed the previous 1m budget before discord.py logs anything.
HEALTHCHECK --interval=2m --timeout=10s --start-period=5m --retries=3 \
    CMD python -c "import os, sys, time; p = os.environ.get('LOG_FILE', 'hikaru_bot.log'); sys.exit(0 if os.path.exists(p) and time.time() - os.path.getmtime(p) < 600 else 1)"

CMD ["python", "main.py"]