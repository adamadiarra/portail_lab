FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 DB_PATH=/data/portail.db
WORKDIR /app
COPY portail_labs.py index.html ./
RUN mkdir /data
VOLUME /data
EXPOSE 8000
CMD ["python", "portail_labs.py", "--host", "0.0.0.0", "--port", "8000"]
