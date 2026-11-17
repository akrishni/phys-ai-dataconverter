# Distributed Processing for Massive Datasets

## Architecture Overview

### Three-Tier Scaling Strategy

```
Tier 1: Local Parallelization (1-10K episodes)
  └─> --workers flag, ProcessPoolExecutor
  
Tier 2: Cluster Parallelization (10K-1M episodes)
  └─> Episode sharding, distributed workers
  
Tier 3: Cloud-Native (1M+ episodes, production)
  └─> AWS Batch, Ray, Kubernetes orchestration
```

---

## Tier 1: Local Parallelization ⚡

### Implementation: `--workers` Flag

```bash
# Process 8 episodes in parallel
phys-ai-dataconverter recordings/ -o dataset --workers 8

# Auto-detect CPU cores
phys-ai-dataconverter recordings/ -o dataset --workers auto
```

### Architecture

```python
from concurrent.futures import ProcessPoolExecutor, as_completed

def convert_single_episode(episode_file, options):
    """Convert one episode - runs in separate process"""
    # Decode images, align timeline, encode video
    return episode_report

def convert_parallel(files, output, options, workers):
    """Process multiple episodes in parallel"""
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(convert_single_episode, f, options): f 
                   for f in files}
        
        for future in as_completed(futures):
            result = future.result()
            yield result
```

**Benefits:**
- ✅ Linear speedup (8 cores = 8× faster)
- ✅ Memory isolated per process
- ✅ Fault tolerant (one failure doesn't crash all)
- ✅ Simple to implement

**Limitations:**
- ❌ Limited to single machine
- ❌ No cross-machine coordination
- ❌ Memory × workers (can exhaust RAM)

---

## Tier 2: Cluster Parallelization 🖥️

### Design: Episode Sharding + Distributed Workers

#### A. Episode Manifest Generation

```bash
# Step 1: Generate manifest with episode metadata
phys-ai-dataconverter manifest recordings/ -o episodes.json

# episodes.json:
{
  "episodes": [
    {"path": "recordings/ep_000.mcap", "index": 0, "size_mb": 120},
    {"path": "recordings/ep_001.mcap", "index": 1, "size_mb": 115},
    ...
  ],
  "total_episodes": 100000,
  "total_size_gb": 12000
}
```

#### B. Shard Assignment

```python
# Distribute episodes across N workers
def shard_episodes(manifest, num_workers):
    """Balance load across workers by file size"""
    episodes = sorted(manifest['episodes'], key=lambda e: e['size_mb'], reverse=True)
    shards = [[] for _ in range(num_workers)]
    shard_sizes = [0] * num_workers
    
    # Greedy bin packing (largest first)
    for ep in episodes:
        min_shard = shard_sizes.index(min(shard_sizes))
        shards[min_shard].append(ep)
        shard_sizes[min_shard] += ep['size_mb']
    
    return shards
```

#### C. Worker Execution

```bash
# Worker 0 processes episodes 0-999
phys-ai-dataconverter convert \
  --manifest episodes.json \
  --shard 0/100 \
  -o dataset_shard_0

# Worker 1 processes episodes 1000-1999
phys-ai-dataconverter convert \
  --manifest episodes.json \
  --shard 1/100 \
  -o dataset_shard_1
```

#### D. Dataset Merging

```python
# LeRobot v3 supports sharded datasets natively
# Just reference all shard directories
dataset = LeRobotDataset(
    "dataset",
    shard_dirs=["dataset_shard_0", "dataset_shard_1", ...]
)

# Or merge metadata explicitly
phys-ai-dataconverter merge \
  dataset_shard_* \
  -o dataset_final
```

---

## Tier 3: Cloud-Native Orchestration ☁️

### Option A: AWS Batch

#### Architecture

```
S3 Input Bucket (MCAP files)
    ↓
AWS Batch Job Array (1000 jobs)
    ↓ (each job processes 100 episodes)
S3 Output Bucket (LeRobot datasets)
    ↓
Lambda/Step Functions (merge metadata)
    ↓
Final Dataset
```

#### Implementation

**1. Docker Container**
```dockerfile
FROM python:3.11-slim

RUN pip install phys-ai-dataconverter

WORKDIR /app

COPY batch_worker.py .

CMD ["python", "batch_worker.py"]
```

**2. Worker Script**
```python
# batch_worker.py
import os
import boto3
from phys_ai_dataconverter import convert, ConvertOptions

def main():
    array_index = int(os.environ['AWS_BATCH_JOB_ARRAY_INDEX'])
    array_size = int(os.environ['AWS_BATCH_JOB_ARRAY_SIZE'])
    
    s3 = boto3.client('s3')
    
    # Download manifest
    s3.download_file('my-bucket', 'episodes.json', 'episodes.json')
    with open('episodes.json') as f:
        manifest = json.load(f)
    
    # Calculate shard
    episodes = manifest['episodes']
    episodes_per_job = len(episodes) // array_size
    start = array_index * episodes_per_job
    end = start + episodes_per_job
    my_episodes = episodes[start:end]
    
    # Download MCAP files
    local_files = []
    for ep in my_episodes:
        local_path = f"/tmp/{os.path.basename(ep['path'])}"
        s3.download_file('my-bucket', ep['path'], local_path)
        local_files.append(local_path)
    
    # Convert
    output_dir = f"/tmp/dataset_shard_{array_index}"
    convert(local_files, output_dir, ConvertOptions(...))
    
    # Upload results
    for root, dirs, files in os.walk(output_dir):
        for file in files:
            local_path = os.path.join(root, file)
            s3_key = f"output/shard_{array_index}/{file}"
            s3.upload_file(local_path, 'my-bucket', s3_key)

if __name__ == '__main__':
    main()
```

**3. Submit Job**
```python
import boto3

batch = boto3.client('batch')

response = batch.submit_job(
    jobName='convert-dataset',
    jobQueue='phys-ai-queue',
    jobDefinition='phys-ai-converter',
    arrayProperties={'size': 1000},  # 1000 parallel workers
    containerOverrides={
        'vcpus': 2,
        'memory': 4096
    }
)
```

**Cost:** ~$50 for 1M episodes (spot instances)

---

### Option B: Ray Cluster

```python
import ray
from phys_ai_dataconverter import convert, ConvertOptions

ray.init(address='auto')  # Connect to Ray cluster

@ray.remote
def convert_episode_batch(episode_files, options):
    """Convert batch of episodes on one Ray worker"""
    return convert(episode_files, '/tmp/output', options)

# Distribute across cluster
episode_batches = chunk_episodes(all_files, batch_size=100)
futures = [convert_episode_batch.remote(batch, options) 
           for batch in episode_batches]

results = ray.get(futures)
```

**Setup:**
```bash
# Start Ray cluster on AWS/GCP/Azure
ray up ray-cluster.yaml

# Submit job
ray submit ray-cluster.yaml convert_job.py
```

---

### Option C: Kubernetes Jobs

```yaml
# converter-job.yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: phys-ai-converter
spec:
  parallelism: 100        # 100 parallel pods
  completions: 100        # 100 total jobs
  template:
    spec:
      restartPolicy: Never
      containers:
      - name: converter
        image: phys-ai-dataconverter:latest
        env:
        - name: JOB_COMPLETION_INDEX
          valueFrom:
            fieldRef:
              fieldPath: metadata.annotations['batch.kubernetes.io/job-completion-index']
        - name: SHARD_SIZE
          value: "1000"
        command:
        - python
        - /app/k8s_worker.py
        resources:
          requests:
            memory: "4Gi"
            cpu: "2"
          limits:
            memory: "8Gi"
            cpu: "4"
```

```bash
kubectl apply -f converter-job.yaml
kubectl logs -f job/phys-ai-converter
```

---

## Progress Tracking & Resumability 📊

### Design: Progress File

```python
# progress.json - written incrementally
{
  "started_at": "2024-11-17T10:00:00Z",
  "total_episodes": 100000,
  "completed": [
    {"path": "ep_000.mcap", "frames": 1800, "duration_s": 1.2},
    {"path": "ep_001.mcap", "frames": 1750, "duration_s": 1.1}
  ],
  "failed": [
    {"path": "ep_042.mcap", "error": "Gap in joint state at 5.2s"}
  ],
  "in_progress": ["ep_003.mcap"],
  "pending": 99995
}
```

### Resume from Checkpoint

```bash
# Initial run (fails after 1000 episodes)
phys-ai-dataconverter recordings/ -o dataset --progress progress.json

# Resume from where it left off
phys-ai-dataconverter recordings/ -o dataset --resume progress.json
```

### Implementation

```python
def convert_with_progress(files, output, options, progress_file=None):
    """Convert with progress tracking"""
    progress = load_progress(progress_file) if progress_file else {}
    completed = set(progress.get('completed', []))
    
    for file in files:
        if file in completed:
            print(f"Skipping {file} (already done)")
            continue
        
        try:
            result = convert_single_episode(file, options)
            progress['completed'].append(file)
            save_progress(progress, progress_file)
        except Exception as e:
            progress['failed'].append({'path': file, 'error': str(e)})
            save_progress(progress, progress_file)
            if options.fail_fast:
                raise
```

---

## Memory Optimization 💾

### Problem: Large Episodes (4K video, long duration)

**Current:** Full episode in memory during encoding
**Issue:** 10-minute 4K episode = ~50 GB RAM

### Solution: Streaming Processing

```python
class StreamingEpisodeProcessor:
    """Process episode in chunks to limit memory"""
    
    def __init__(self, chunk_size=1000):
        self.chunk_size = chunk_size  # frames per chunk
    
    def process(self, episode_file, output):
        # Read episode metadata first
        frame_count = self.get_frame_count(episode_file)
        
        # Process in chunks
        for chunk_start in range(0, frame_count, self.chunk_size):
            chunk_end = min(chunk_start + self.chunk_size, frame_count)
            
            # Load only this chunk into memory
            frames = self.read_frames(episode_file, chunk_start, chunk_end)
            
            # Encode chunk
            self.encode_chunk(frames, output)
            
            # Free memory
            del frames
            gc.collect()
```

---

## Monitoring & Observability 📈

### Metrics to Track

```python
import prometheus_client as prom

# Counters
episodes_processed = prom.Counter('episodes_processed_total', 'Total episodes')
episodes_failed = prom.Counter('episodes_failed_total', 'Failed episodes')

# Gauges  
episodes_in_progress = prom.Gauge('episodes_in_progress', 'Currently processing')

# Histograms
episode_duration = prom.Histogram('episode_duration_seconds', 'Processing time')
episode_frames = prom.Histogram('episode_frames', 'Frame count')

# Usage
with episode_duration.time():
    result = convert_episode(file)
    episodes_processed.inc()
```

### Dashboard (Grafana)

```
Total Episodes:     100,000
Completed:          45,234 (45.2%)
Failed:             12 (0.01%)
In Progress:        156
Remaining:          54,598

Throughput:         1,234 episodes/hour
ETA:                ~44 hours

Average Duration:   2.1 seconds/episode
P95 Duration:       5.3 seconds
P99 Duration:       12.1 seconds
```

---

## Error Handling & Retry Logic 🔄

```python
from tenacity import retry, stop_after_attempt, wait_exponential

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=60),
    reraise=True
)
def convert_with_retry(episode_file, options):
    """Retry failed conversions with exponential backoff"""
    try:
        return convert_single_episode(episode_file, options)
    except ConversionError as e:
        # Don't retry unrecoverable errors
        if "not a file" in str(e) or "not supported" in str(e):
            raise
        # Retry transient errors
        print(f"Retrying {episode_file}: {e}")
        raise
```

---

## Cost Optimization 💰

### Spot Instances / Preemptible VMs

```python
# AWS Batch with spot instances (70% discount)
batch.submit_job(
    ...,
    parameters={
        'spot_instance': 'true',
        'max_spot_interruption_retries': '3'
    }
)
```

### Right-Sizing Workers

| Dataset Characteristics | Recommended Instance | Cost/Hour |
|-------------------------|---------------------|-----------|
| Many small episodes (< 60s) | c6i.large (2 vCPU, 4 GB) | $0.085 |
| Medium episodes (1-5 min) | c6i.xlarge (4 vCPU, 8 GB) | $0.17 |
| Large episodes (> 5 min) | c6i.2xlarge (8 vCPU, 16 GB) | $0.34 |
| 4K video | c6i.4xlarge (16 vCPU, 32 GB) | $0.68 |

### Auto-Scaling

```python
# Ray Cluster auto-scaling
ray.init(
    address='auto',
    _system_config={
        'autoscaling_mode': 'enabled',
        'min_workers': 10,
        'max_workers': 1000,
        'idle_timeout_minutes': 5
    }
)
```

---

## Reference Architectures

### 1. Simple: GNU Parallel

```bash
# For 10K-100K episodes on a single powerful machine
find recordings/ -name "*.mcap" | \
  parallel -j 32 \
  "phys-ai-dataconverter {} -o dataset_{#}"
```

**Cost:** $0 (your machine)
**Time:** Hours to days
**Complexity:** ⭐ (very simple)

### 2. Medium: Ray on AWS

```bash
# For 100K-1M episodes
ray up ray-cluster.yaml         # Launch cluster
ray submit ray-cluster.yaml convert.py   # Submit job
ray down ray-cluster.yaml       # Teardown
```

**Cost:** $50-500
**Time:** Minutes to hours  
**Complexity:** ⭐⭐ (moderate)

### 3. Enterprise: Kubernetes + Argo Workflows

```bash
# For 1M+ episodes, production pipelines
kubectl apply -f converter-workflow.yaml
argo submit converter-workflow.yaml --watch
```

**Cost:** Varies (managed K8s cluster)
**Time:** Minutes
**Complexity:** ⭐⭐⭐⭐ (complex)

---

## Implementation Roadmap

### Phase 1: Local Parallelization (v0.3) ✅ Planned
- [ ] `--workers` flag
- [ ] ProcessPoolExecutor implementation
- [ ] Progress bar with ETA
- [ ] Basic progress file support

### Phase 2: Resumability (v0.4)
- [ ] `--progress` and `--resume` flags
- [ ] Checkpoint after each episode
- [ ] Failed episode tracking
- [ ] Retry logic

### Phase 3: Cloud Integration (v0.5)
- [ ] `manifest` command (generate episode list)
- [ ] `--shard` flag for distributed workers
- [ ] `merge` command for combining shards
- [ ] AWS Batch example
- [ ] Ray integration example
- [ ] Kubernetes example

### Phase 4: Enterprise (v1.0)
- [ ] Prometheus metrics
- [ ] Grafana dashboards
- [ ] Streaming processing (memory optimization)
- [ ] GPU encoding support
- [ ] Production-grade error handling

---

## Quick Start Examples

### Local (< 10K episodes)
```bash
phys-ai-dataconverter recordings/ -o dataset --workers 8
```

### Cloud (100K episodes, AWS)
```bash
# 1. Upload to S3
aws s3 sync recordings/ s3://my-bucket/recordings/

# 2. Generate manifest
phys-ai-dataconverter manifest s3://my-bucket/recordings/ > episodes.json
aws s3 cp episodes.json s3://my-bucket/

# 3. Submit AWS Batch job
python submit_batch_job.py --episodes episodes.json --shards 100

# 4. Wait for completion
python monitor_batch_job.py --job-id abc123

# 5. Download results
aws s3 sync s3://my-bucket/output/ dataset/
```

### Ray Cluster (1M episodes)
```bash
# 1. Launch cluster
ray up ray-cluster.yaml

# 2. Submit job
ray submit ray-cluster.yaml convert_job.py \
  --recordings=recordings/ \
  --output=dataset/

# 3. Monitor
ray dashboard  # Open http://localhost:8265

# 4. Teardown
ray down ray-cluster.yaml
```

---

## Summary

**Massive Dataset Support = Layered Approach**

1. **Local Parallelization** (Phase 1)
   - `--workers` flag for multi-core
   - ~8× speedup on typical machines
   - Good for 1-10K episodes

2. **Cluster Distribution** (Phase 2)
   - Episode sharding + distributed workers
   - ~100× speedup with 100 machines
   - Good for 10K-1M episodes

3. **Cloud-Native** (Phase 3)
   - AWS Batch, Ray, Kubernetes
   - ~1000× speedup with auto-scaling
   - Good for 1M+ episodes, production

**Next Steps:**
1. Implement `--workers` flag (local parallelization)
2. Add progress tracking and resume
3. Create cloud integration examples
4. Performance testing at scale
