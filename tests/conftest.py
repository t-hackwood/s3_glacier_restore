import os

import boto3
import pytest
from moto import mock_aws

from thaw.config import Config

REGION = "ap-southeast-2"
SRC = "archive-src"
BASE = "PROJECTS/DEMO_PROJECT/"

# A typical project folder: mixed DEEP_ARCHIVE / STANDARD objects and folder markers.
LAYOUT = {
    "ANALYSIS/": ("STANDARD", 0),
    "ANALYSIS/model.tif": ("DEEP_ARCHIVE", 2000),
    "DOCUMENTATION/": ("STANDARD", 0),
    "DOCUMENTATION/report.pdf": ("DEEP_ARCHIVE", 300),
    "DOCUMENTATION/notes.txt": ("DEEP_ARCHIVE", 50),
    "DOCUMENTATION/sub/deep.csv": ("DEEP_ARCHIVE", 70),
    "FINAL_DELIVERABLES/out.tif": ("DEEP_ARCHIVE", 900),
    "FINAL_DELIVERABLES/out.tif.aux.xml": ("DEEP_ARCHIVE", 10),
    "IMAGES/photo.jpg": ("STANDARD", 120),
    "PROPOSAL/proposal.docx": ("DEEP_ARCHIVE", 80),
    "readme.txt": ("STANDARD", 5),
}


@pytest.fixture(autouse=True)
def aws_env(monkeypatch, tmp_path):
    for k in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("THAW_HOME", str(tmp_path))
    monkeypatch.setenv("THAW_CONFIG", str(tmp_path / "none.toml"))


@pytest.fixture
def s3():
    with mock_aws():
        c = boto3.client("s3", region_name=REGION)
        for b in (SRC, "dest-same", ):
            c.create_bucket(Bucket=b, CreateBucketConfiguration={"LocationConstraint": REGION})
        boto3.client("s3", region_name="us-west-2").create_bucket(
            Bucket="dest-other", CreateBucketConfiguration={"LocationConstraint": "us-west-2"})
        for rel, (sc, size) in LAYOUT.items():
            c.put_object(Bucket=SRC, Key=BASE + rel, Body=b"x" * size, StorageClass=sc)
        c.put_object(Bucket=SRC, Key="PROJECTS/OTHER/x.txt", Body=b"x", StorageClass="DEEP_ARCHIVE")
        yield c


@pytest.fixture
def cfg(tmp_path):
    return Config.load()
