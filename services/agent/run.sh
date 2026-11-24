#!/bin/bash
# Lambda Web Adapter starts this script, waits for /healthz, then proxies
# API Gateway invocations to uvicorn and streams the response back.
PATH=$PATH:$LAMBDA_TASK_ROOT/bin \
  PYTHONPATH=$LAMBDA_TASK_ROOT:$PYTHONPATH:/opt/python:$LAMBDA_RUNTIME_DIR \
  exec python3 -m uvicorn --host 127.0.0.1 --port="${AWS_LWA_PORT:-8080}" --no-access-log app:app
