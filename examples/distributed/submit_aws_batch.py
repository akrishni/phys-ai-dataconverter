#!/usr/bin/env python3
"""
Submit distributed conversion job to AWS Batch.

Usage:
    python submit_aws_batch.py --manifest episodes.json --shards 100
"""

import argparse
import json
import sys

import boto3


def upload_manifest(s3_client, bucket, manifest_file):
    """Upload manifest to S3"""
    key = 'converter-jobs/episodes.json'
    s3_client.upload_file(manifest_file, bucket, key)
    return key


def submit_job(batch_client, job_name, queue, job_def, array_size, env_vars):
    """Submit AWS Batch array job"""
    response = batch_client.submit_job(
        jobName=job_name,
        jobQueue=queue,
        jobDefinition=job_def,
        arrayProperties={'size': array_size},
        containerOverrides={
            'environment': [
                {'name': k, 'value': v} for k, v in env_vars.items()
            ]
        }
    )
    return response['jobId']


def main():
    parser = argparse.ArgumentParser(description='Submit distributed conversion to AWS Batch')
    parser.add_argument('--manifest', required=True, help='Path to episodes.json manifest')
    parser.add_argument('--shards', type=int, default=100, help='Number of parallel workers')
    parser.add_argument('--bucket', required=True, help='S3 bucket name')
    parser.add_argument('--queue', default='phys-ai-queue', help='AWS Batch job queue')
    parser.add_argument('--job-definition', default='phys-ai-converter', help='Job definition name')
    args = parser.parse_args()

    # Load manifest to validate
    with open(args.manifest) as f:
        manifest = json.load(f)

    print(f"Manifest: {len(manifest['episodes'])} episodes, {manifest.get('total_size_gb', '?')} GB")
    print(f"Shards: {args.shards} workers")
    print(f"Episodes per shard: ~{len(manifest['episodes']) // args.shards}")

    # Initialize AWS clients
    s3_client = boto3.client('s3')
    batch_client = boto3.client('batch')

    # Upload manifest
    print("\nUploading manifest to S3...")
    manifest_key = upload_manifest(s3_client, args.bucket, args.manifest)
    print(f"✓ Uploaded to s3://{args.bucket}/{manifest_key}")

    # Submit job
    print(f"\nSubmitting job to AWS Batch queue '{args.queue}'...")
    job_id = submit_job(
        batch_client,
        job_name='phys-ai-convert',
        queue=args.queue,
        job_def=args.job_definition,
        array_size=args.shards,
        env_vars={
            'S3_BUCKET': args.bucket,
            'MANIFEST_KEY': manifest_key,
            'OUTPUT_PREFIX': 'output'
        }
    )

    print(f"✓ Job submitted: {job_id}")
    print(f"\nMonitor at: https://console.aws.amazon.com/batch/v2/home#jobs/array/{job_id}")
    print(f"Or run: aws batch describe-jobs --jobs {job_id}")

    return 0


if __name__ == '__main__':
    sys.exit(main())
