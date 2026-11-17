#!/usr/bin/env python3
"""
AWS Batch worker for distributed episode conversion.

This script runs as a container in AWS Batch, processing a shard of episodes.

Environment variables:
    AWS_BATCH_JOB_ARRAY_INDEX: Worker ID (0-based)
    AWS_BATCH_JOB_ARRAY_SIZE: Total number of workers
    S3_BUCKET: S3 bucket name for input/output
    MANIFEST_KEY: S3 key for episodes.json manifest
    OUTPUT_PREFIX: S3 prefix for output shards
"""

import json
import os
import sys
from pathlib import Path

import boto3

# Add converter to path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from phys_ai_dataconverter import ConvertOptions, convert


def download_manifest(s3_client, bucket, key):
    """Download and parse episode manifest from S3"""
    local_path = "/tmp/episodes.json"
    s3_client.download_file(bucket, key, local_path)
    with open(local_path) as f:
        return json.load(f)


def calculate_shard(episodes, shard_index, shard_count):
    """Calculate which episodes this worker should process"""
    episodes_per_shard = len(episodes) // shard_count
    start = shard_index * episodes_per_shard

    # Last shard gets any remainder
    if shard_index == shard_count - 1:
        end = len(episodes)
    else:
        end = start + episodes_per_shard

    return episodes[start:end]


def download_episodes(s3_client, bucket, episodes):
    """Download MCAP files from S3 to local disk"""
    local_files = []

    for ep in episodes:
        s3_key = ep['path']
        local_path = f"/tmp/{Path(s3_key).name}"

        print(f"Downloading {s3_key}...")
        s3_client.download_file(bucket, s3_key, local_path)
        local_files.append(Path(local_path))

    return local_files


def upload_shard(s3_client, bucket, output_dir, prefix, shard_index):
    """Upload converted dataset shard to S3"""
    for root, dirs, files in os.walk(output_dir):
        for file in files:
            local_path = Path(root) / file
            relative_path = local_path.relative_to(output_dir)
            s3_key = f"{prefix}/shard_{shard_index:04d}/{relative_path}"

            print(f"Uploading {s3_key}...")
            s3_client.upload_file(str(local_path), bucket, s3_key)


def main():
    # Get environment variables
    shard_index = int(os.environ.get('AWS_BATCH_JOB_ARRAY_INDEX', '0'))
    shard_count = int(os.environ.get('AWS_BATCH_JOB_ARRAY_SIZE', '1'))
    bucket = os.environ['S3_BUCKET']
    manifest_key = os.environ['MANIFEST_KEY']
    output_prefix = os.environ.get('OUTPUT_PREFIX', 'output')

    print(f"Worker {shard_index + 1}/{shard_count} starting...")

    # Initialize S3 client
    s3_client = boto3.client('s3')

    # Download manifest
    print("Downloading manifest...")
    manifest = download_manifest(s3_client, bucket, manifest_key)

    # Calculate shard
    my_episodes = calculate_shard(manifest['episodes'], shard_index, shard_count)
    print(f"Processing {len(my_episodes)} episodes (indices {my_episodes[0]['index']}-{my_episodes[-1]['index']})")

    # Download episode files
    local_files = download_episodes(s3_client, bucket, my_episodes)

    # Convert
    output_dir = Path(f"/tmp/dataset_shard_{shard_index}")
    print(f"Converting to {output_dir}...")

    try:
        options = ConvertOptions(
            task=manifest.get('task', 'teleoperation'),
            vcodec='libsvtav1',
            crf=30
        )
        report = convert(local_files, output_dir, options)

        print(f"✓ Converted {len(report.episodes)} episodes, {report.total_frames} frames")

        # Upload results
        upload_shard(s3_client, bucket, output_dir, output_prefix, shard_index)

        print(f"✓ Worker {shard_index + 1} complete!")
        return 0

    except Exception as e:
        print(f"✗ Worker {shard_index + 1} failed: {e}", file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
