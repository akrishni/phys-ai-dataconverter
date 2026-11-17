# Distributed Processing Examples

This directory contains reference implementations for processing massive datasets at scale.

## Quick Start

### Local Parallelization (1-10K episodes)

```bash
# Use all CPU cores
phys-ai-dataconverter recordings/ -o dataset --workers auto

# Or specific number
phys-ai-dataconverter recordings/ -o dataset --workers 8
```

**Note:** `--workers` flag planned for v0.3. For now, use GNU parallel:

```bash
find recordings/ -name "*.mcap" | \
  parallel -j 8 "phys-ai-dataconverter {} -o dataset_{#}"
```

---

## AWS Batch (10K-1M episodes)

### 1. Build and Push Docker Image

```bash
# Build
docker build -t phys-ai-converter .

# Tag and push to ECR
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin <account-id>.dkr.ecr.us-east-1.amazonaws.com
docker tag phys-ai-converter:latest <account-id>.dkr.ecr.us-east-1.amazonaws.com/phys-ai-converter:latest
docker push <account-id>.dkr.ecr.us-east-1.amazonaws.com/phys-ai-converter:latest
```

### 2. Create Job Definition

```bash
aws batch register-job-definition \
  --job-definition-name phys-ai-converter \
  --type container \
  --container-properties '{
    "image": "<account-id>.dkr.ecr.us-east-1.amazonaws.com/phys-ai-converter:latest",
    "vcpus": 2,
    "memory": 4096,
    "jobRoleArn": "arn:aws:iam::<account-id>:role/BatchJobRole",
    "executionRoleArn": "arn:aws:iam::<account-id>:role/BatchExecutionRole"
  }'
```

### 3. Generate Manifest

```python
# create_manifest.py
import json
from pathlib import Path

recordings = Path("recordings/")
episodes = [
    {
        "path": f"recordings/{f.name}",
        "index": i,
        "size_mb": f.stat().st_size / 1e6
    }
    for i, f in enumerate(sorted(recordings.glob("*.mcap")))
]

manifest = {
    "episodes": episodes,
    "total_episodes": len(episodes),
    "total_size_gb": sum(e["size_mb"] for e in episodes) / 1000,
    "task": "pick up bottles"
}

with open("episodes.json", "w") as f:
    json.dump(manifest, f, indent=2)

print(f"Created manifest: {len(episodes)} episodes, {manifest['total_size_gb']:.1f} GB")
```

### 4. Upload Data to S3

```bash
aws s3 sync recordings/ s3://my-bucket/recordings/
aws s3 cp episodes.json s3://my-bucket/manifest/episodes.json
```

### 5. Submit Job

```bash
python submit_aws_batch.py \
  --manifest episodes.json \
  --shards 100 \
  --bucket my-bucket
```

### 6. Monitor

```bash
# Get job status
aws batch describe-jobs --jobs <job-id>

# View logs
aws logs tail /aws/batch/job --follow
```

### 7. Download Results

```bash
aws s3 sync s3://my-bucket/output/ dataset/
```

**Cost estimate:** ~$0.50 per 10K episodes (spot instances)

---

## Ray Cluster (100K-1M episodes)

### 1. Install Ray

```bash
pip install "ray[default]"
```

### 2. Launch Cluster

**Local testing:**
```bash
ray start --head --port=6379
```

**AWS (auto-scaling):**
```yaml
# ray-cluster.yaml
cluster_name: phys-ai-converter

max_workers: 100

provider:
  type: aws
  region: us-east-1
  
head_node_type: head
available_node_types:
  head:
    node_config:
      InstanceType: m5.xlarge
  worker:
    node_config:
      InstanceType: c5.2xlarge
      InstanceMarketOptions:
        MarketType: spot
    min_workers: 10
    max_workers: 100
```

```bash
ray up ray-cluster.yaml
```

### 3. Submit Job

```bash
ray submit ray-cluster.yaml ray_cluster.py recordings/ dataset/ 10
```

### 4. Monitor

```bash
# Dashboard
ray dashboard
# Opens http://localhost:8265

# Logs
ray logs
```

### 5. Teardown

```bash
ray down ray-cluster.yaml
```

**Cost estimate:** ~$5 per 100K episodes (auto-scaled spot instances)

---

## Kubernetes (Enterprise / Production)

### 1. Create Job Manifest

```yaml
# converter-job.yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: phys-ai-converter
spec:
  parallelism: 100
  completions: 100
  template:
    spec:
      restartPolicy: Never
      containers:
      - name: converter
        image: <registry>/phys-ai-converter:latest
        env:
        - name: SHARD_INDEX
          valueFrom:
            fieldRef:
              fieldPath: metadata.annotations['batch.kubernetes.io/job-completion-index']
        - name: SHARD_COUNT
          value: "100"
        - name: S3_BUCKET
          value: "my-bucket"
        resources:
          requests:
            memory: "4Gi"
            cpu: "2"
          limits:
            memory: "8Gi"
            cpu: "4"
```

### 2. Deploy

```bash
kubectl apply -f converter-job.yaml
```

### 3. Monitor

```bash
# Job status
kubectl get jobs

# Pod logs
kubectl logs -f job/phys-ai-converter

# Failed pods
kubectl get pods --field-selector=status.phase=Failed
```

### 4. Cleanup

```bash
kubectl delete job phys-ai-converter
```

---

## Performance Comparison

| Method | Setup Time | Processing Time (100K episodes) | Cost | Complexity |
|--------|------------|--------------------------------|------|------------|
| Local (8 cores) | 0 min | ~40 hours | $0 | ⭐ |
| GNU Parallel | 0 min | ~5 hours | $0 | ⭐ |
| AWS Batch | 30 min | ~10 min | $5 | ⭐⭐ |
| Ray Cluster | 15 min | ~10 min | $8 | ⭐⭐⭐ |
| Kubernetes | 60 min | ~10 min | $10 | ⭐⭐⭐⭐ |

---

## Troubleshooting

### AWS Batch: Job stuck in RUNNABLE

```bash
# Check queue
aws batch describe-job-queues --job-queues phys-ai-queue

# Check compute environment
aws batch describe-compute-environments

# Common issue: insufficient capacity
# Solution: Use spot instances with multiple instance types
```

### Ray: Workers not connecting

```bash
# Check firewall rules (port 6379)
# Verify head node IP
ray status  # On head node

# Restart workers
ray stop  # On worker
ray start --address=<head-ip>:6379
```

### Kubernetes: Pods being evicted

```bash
# Check resource limits
kubectl describe pod <pod-name>

# Increase memory limits in job spec
resources:
  limits:
    memory: "16Gi"  # Increase if needed
```

---

## Cost Optimization Tips

1. **Use spot instances** (70% discount)
2. **Right-size workers** (don't over-provision)
3. **Batch episodes** (reduce overhead)
4. **Use H.264** instead of AV1 (2× faster)
5. **Auto-scale** (shut down when idle)

---

## Next Steps

- See [DISTRIBUTED_PROCESSING.md](../../DISTRIBUTED_PROCESSING.md) for architecture details
- See [PERFORMANCE_SCALING.md](../../PERFORMANCE_SCALING.md) for sizing guidance
- Join discussions at https://github.com/akrishni/phys-ai-dataconverter/discussions
