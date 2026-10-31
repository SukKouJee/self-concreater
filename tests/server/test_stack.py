"""테스트 스택(deploy/server/docker-compose.test.yml)이 PG 16과 S3 API를 내주는지(T5). T6·T7의 바닥."""
from __future__ import annotations

import uuid

import pytest

from tests.server.conftest import Stack

psycopg = pytest.importorskip("psycopg")


def test_stack_postgres_is_16(pg_stack: Stack) -> None:
    with psycopg.connect(pg_stack.dsn("ytc")) as conn:
        assert conn.info.server_version // 10000 == 16


def test_stack_minio_speaks_s3(pg_stack: Stack) -> None:
    boto3 = pytest.importorskip("boto3")
    if not pg_stack.s3_endpoint:
        pytest.skip("YTC_TEST_S3_ENDPOINT가 없다")
    s3 = boto3.client("s3", endpoint_url=pg_stack.s3_endpoint, aws_access_key_id=pg_stack.s3_access_key,
                      aws_secret_access_key=pg_stack.s3_secret_key, region_name="us-east-1")
    bucket = f"t5-{uuid.uuid4().hex[:12]}"
    s3.create_bucket(Bucket=bucket)
    try:
        s3.put_object(Bucket=bucket, Key="users/u/a.bin", Body=b"\xef\xbb\xbfhi\r\n")
        assert s3.get_object(Bucket=bucket, Key="users/u/a.bin")["Body"].read() == b"\xef\xbb\xbfhi\r\n"
        part = s3.get_object(Bucket=bucket, Key="users/u/a.bin", Range="bytes=3-4")
        assert (part["ResponseMetadata"]["HTTPStatusCode"], part["Body"].read()) == (206, b"hi")
    finally:
        s3.delete_object(Bucket=bucket, Key="users/u/a.bin")
        s3.delete_bucket(Bucket=bucket)
